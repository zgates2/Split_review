"""Audit an unfamiliar v3 dataset against the v3.0 contract before reviewing it.

Use this the first time a new vendor converter's dataset arrives: it lists
non-standard columns, codec/GOP facts and per-episode bookkeeping mismatches.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from . import video
from .dataset import V3Dataset

STANDARD_DATA_COLUMNS = {
    "observation.state", "action", "timestamp", "frame_index", "episode_index",
    "index", "task_index",
}
STANDARD_EPISODE_PREFIXES = ("videos/", "stats/", "meta/episodes/")


def audit(root: Path) -> dict:
    ds = V3Dataset(root)
    info = ds.info
    ep_table = ds.episodes_table
    data_table = pq.read_table(ds.data_file(ds.episodes[0]))
    extra_data = [c for c in data_table.column_names if c not in STANDARD_DATA_COLUMNS]
    core_cols = [c for c in ep_table.column_names if not c.startswith(STANDARD_EPISODE_PREFIXES)]
    codes = {}
    gops = {}
    for key in ds.video_keys:
        ref = ds.episode(0).videos[key]
        path = ds.video_file(ref)
        facts = video.probe(path)
        keys = facts["keyframes"]
        diffs = [round(b - a, 3) for a, b in zip(keys, keys[1:])]
        codes[key] = {k: facts[k] for k in ("codec", "width", "height", "fps", "frames")}
        gops[key] = {
            "keyframes": len(keys),
            "median_gop_s": sorted(diffs)[len(diffs) // 2] if diffs else None,
            "max_gop_s": max(diffs) if diffs else None,
        }
    mismatches = []
    for ep in ds.episodes:
        for key, ref in ep.videos.items():
            frames = int(round(ref.duration * ds.fps))
            if abs(frames - ep.length) > 1:
                mismatches.append(
                    {"episode": ep.index, "camera": key, "rows": ep.length, "video_frames": frames}
                )
    return {
        "root": str(ds.root),
        "codebase_version": info.get("codebase_version"),
        "fps": ds.fps,
        "episodes": ds.n_episodes,
        "cameras": ds.video_keys,
        "tasks": sorted(set(ds.tasks.values())),
        "data_columns": data_table.column_names,
        "non_standard_data_columns": extra_data,
        "episode_core_columns": core_cols,
        "video": codes,
        "gop": gops,
        "row_video_mismatches": mismatches[:20],
        "row_video_mismatch_count": len(mismatches),
    }
