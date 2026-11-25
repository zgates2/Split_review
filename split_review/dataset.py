"""Read a LeRobot v3.0 dataset: metadata, episodes, data rows, video references.

Everything is driven by ``meta/info.json`` + the episodes table, so the tool is
not tied to a fixed number of cameras, a fixed state dimension, or a specific
codec. Only the on-disk v3 layout is assumed (which is the contract).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

VIDEO_DTYPES = {"video", "image"}


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoRef:
    key: str
    chunk: int
    file: int
    from_ts: float
    to_ts: float

    @property
    def duration(self) -> float:
        return max(0.0, self.to_ts - self.from_ts)


@dataclass
class Episode:
    index: int
    length: int
    tasks: list[str]
    videos: dict[str, VideoRef]
    from_index: int
    to_index: int
    data_chunk: int
    data_file: int


class V3Dataset:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        info_file = self.root / "meta" / "info.json"
        if not info_file.is_file():
            raise DatasetError(f"not a LeRobot dataset (no meta/info.json): {self.root}")
        self.info = json.loads(info_file.read_text(encoding="utf-8"))
        version = str(self.info.get("codebase_version", ""))
        if not version.startswith("v3"):
            raise DatasetError(f"only codebase_version v3.x is supported, got {version!r}")
        self.fps = float(self.info.get("fps") or 0) or 30.0
        self.features: dict = self.info.get("features") or {}
        self.data_path = self.info.get(
            "data_path", "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        )
        self.video_path_tmpl = self.info.get("video_path")
        self.video_keys = [k for k, v in self.features.items() if v.get("dtype") in VIDEO_DTYPES]
        self.tasks = self._read_tasks()
        self.episodes_table = self._read_episodes()
        self.episodes = self._build_episodes()

    # ------------------------------------------------------------------ meta
    def _read_tasks(self) -> dict[int, str]:
        path = self.root / "meta" / "tasks.parquet"
        if not path.is_file():
            raise DatasetError(f"missing meta/tasks.parquet in {self.root}")
        table = pq.read_table(path)
        if "task" in table.column_names and "task_index" in table.column_names:
            tasks = table.column("task").to_pylist()
            idx = table.column("task_index").to_pylist()
        else:  # index stored as the pandas index
            df = table.to_pandas()
            tasks = list(df.index)
            idx = list(df["task_index"])
        return {int(i): str(t) for t, i in zip(tasks, idx)}

    def _read_episodes(self) -> pa.Table:
        files = sorted((self.root / "meta" / "episodes").rglob("*.parquet"))
        if not files:
            raise DatasetError(f"missing meta/episodes/**/*.parquet in {self.root}")
        tables = [pq.read_table(f) for f in files]
        return pa.concat_tables(tables) if len(tables) > 1 else tables[0]

    def _build_episodes(self) -> list[Episode]:
        df = self.episodes_table.to_pandas()
        out: list[Episode] = []
        for _, row in df.iterrows():
            videos = {}
            for key in self.video_keys:
                videos[key] = VideoRef(
                    key=key,
                    chunk=int(row[f"videos/{key}/chunk_index"]),
                    file=int(row[f"videos/{key}/file_index"]),
                    from_ts=float(row[f"videos/{key}/from_timestamp"]),
                    to_ts=float(row[f"videos/{key}/to_timestamp"]),
                )
            tasks = row["tasks"]
            if isinstance(tasks, str):
                tasks = [tasks]
            out.append(
                Episode(
                    index=int(row["episode_index"]),
                    length=int(row["length"]),
                    tasks=list(tasks),
                    videos=videos,
                    from_index=int(row["dataset_from_index"]),
                    to_index=int(row["dataset_to_index"]),
                    data_chunk=int(row["data/chunk_index"]),
                    data_file=int(row["data/file_index"]),
                )
            )
        out.sort(key=lambda e: e.index)
        return out

    def episode(self, index: int) -> Episode:
        for ep in self.episodes:
            if ep.index == index:
                return ep
        raise DatasetError(f"episode {index} not found in {self.root}")

    # ------------------------------------------------------------------ data
    def data_file(self, episode: Episode) -> Path:
        rel = self.data_path.format(chunk_index=episode.data_chunk, file_index=episode.data_file)
        return self.root / rel

    def episode_rows(self, episode: Episode) -> pa.Table:
        table = pq.read_table(self.data_file(episode))
        mask = pc.and_(
            pc.greater_equal(table.column("index"), episode.from_index),
            pc.less(table.column("index"), episode.to_index),
        )
        rows = table.filter(mask)
        if rows.num_rows != episode.length:
            raise DatasetError(
                f"episode {episode.index}: metadata says length={episode.length} "
                f"but found {rows.num_rows} rows in {self.data_file(episode)}"
            )
        return rows

    def video_file(self, ref: VideoRef) -> Path:
        if not self.video_path_tmpl:
            raise DatasetError("dataset has no video_path template")
        rel = self.video_path_tmpl.format(
            video_key=ref.key, chunk_index=ref.chunk, file_index=ref.file
        )
        path = self.root / rel
        if not path.is_file():
            raise DatasetError(f"missing video file: {path}")
        return path

    # ------------------------------------------------------------------ misc
    @property
    def n_episodes(self) -> int:
        return len(self.episodes)

    def summary(self) -> dict:
        return {
            "root": str(self.root),
            "name": self.root.name,
            "fps": self.fps,
            "n_episodes": self.n_episodes,
            "total_frames": sum(e.length for e in self.episodes),
            "cameras": self.video_keys,
            "tasks": sorted(set(self.tasks.values())),
            "features": {
                k: {"dtype": v.get("dtype"), "shape": v.get("shape")}
                for k, v in self.features.items()
                if v.get("dtype") not in VIDEO_DTYPES
            },
            "robot_type": self.info.get("robot_type"),
        }

    def frame_to_time(self, frame: float) -> float:
        return frame / self.fps

    def time_to_frame(self, seconds: float) -> int:
        return int(round(seconds * self.fps))

    def episode_duration(self, episode: Episode) -> float:
        return episode.length / self.fps


def curves(rows: pa.Table, features: dict, max_points: int = 1200) -> dict:
    """Downsample numeric features for the review UI."""
    out: dict[str, dict] = {}
    n = rows.num_rows
    step = max(1, math.ceil(n / max_points))
    idx = list(range(0, n, step))
    if idx and idx[-1] != n - 1:
        idx.append(n - 1)
    for key, spec in features.items():
        if spec.get("dtype") in VIDEO_DTYPES:
            continue
        if key not in rows.column_names:
            continue
        if spec.get("dtype") in ("float32", "float64", "int64", "int32"):
            col = rows.column(key).combine_chunks()
            values = col.to_pylist()
            if not values:
                continue
            first = values[0]
            if isinstance(first, list):
                arr = [values[i] for i in idx]
            else:
                arr = [[values[i]] for i in idx]
            meta = {
                "names": spec.get("names") or [f"d{i}" for i in range(len(arr[0]))],
                "times": [idx_i / 30.0 for idx_i in idx],  # replaced by caller with fps
                "values": arr,
            }
            out[key] = meta
    return out
