"""Structural + loader verification of a produced v3 dataset."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def structural(root: Path) -> dict:
    root = Path(root)
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    eps = pd.concat(
        [pq.read_table(f).to_pandas() for f in sorted((root / "meta" / "episodes").rglob("*.parquet"))],
        ignore_index=True,
    )
    data_files = sorted((root / "data").rglob("*.parquet"))
    rows = pd.concat([pq.read_table(f).to_pandas() for f in data_files], ignore_index=True)
    videos = sorted((root / "videos").rglob("*.mp4"))
    problems = []
    if int(info["total_episodes"]) != len(eps):
        problems.append(f"total_episodes {info['total_episodes']} != {len(eps)} episode rows")
    if int(info["total_frames"]) != len(rows):
        problems.append(f"total_frames {info['total_frames']} != {len(rows)} data rows")
    for _, ep in eps.iterrows():
        length = int(ep["length"])
        chunk = rows[rows["episode_index"] == int(ep["episode_index"])]
        if len(chunk) != length:
            problems.append(f"episode {ep['episode_index']}: length {length} != {len(chunk)} rows")
        fi = chunk["frame_index"].tolist()
        if fi != list(range(length)):
            problems.append(f"episode {ep['episode_index']}: frame_index not 0..n-1")
    return {
        "root": str(root),
        "episodes": int(len(eps)),
        "frames": int(len(rows)),
        "video_files": len(videos),
        "tasks": pq.read_table(root / "meta" / "tasks.parquet").to_pandas().index.tolist(),
        "problems": problems,
    }


def loadable(root: Path) -> dict:
    """Try the official loader; returns episode lengths it reports."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset(repo_id="local/verify", root=str(root))
    info = {
        "len": len(ds),
        "episodes": int(ds.meta.total_episodes),
        "fps": ds.meta.fps,
        "camera_keys": sorted(ds.meta.camera_keys),
    }
    lengths = []
    for i in range(info["episodes"]):
        start = ds.meta.episodes[i]["dataset_from_index"]
        stop = ds.meta.episodes[i]["dataset_to_index"]
        lengths.append(int(stop - start))
    info["episode_lengths"] = lengths
    return info


def verify(root: Path, sample: bool = True) -> dict:
    out = structural(root)
    try:
        out["loader"] = loadable(root)
    except Exception as exc:  # pragma: no cover - depends on env
        out["loader_error"] = f"{type(exc).__name__}: {exc}"
    if sample and "loader" in out:
        try:
            from lerobot.datasets.lerobot_dataset import LeRobotDataset

            ds = LeRobotDataset(repo_id="local/verify", root=str(root))
            frame = ds[0]
            out["sample_keys"] = sorted(frame.keys())
        except Exception as exc:  # pragma: no cover
            out["sample_error"] = f"{type(exc).__name__}: {exc}"
    out["ok"] = not out["problems"] and ("loader" in out) and not out.get("loader_error")
    return out
