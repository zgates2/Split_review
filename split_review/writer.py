"""Write a LeRobot v3.0 dataset that the official loader accepts.

The output mirrors the schema of the source dataset (same ``info.json``
features, same episodes table columns, same data table schema), so extra
non-standard columns carried by a vendor converter survive the split.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import stats as stats_mod
from .dataset import V3Dataset

DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"

# Columns whose values are derived from the segment itself.
REWRITTEN_COLUMNS = ("index", "episode_index", "frame_index", "task_index", "timestamp")


def _coerce(value, typ):
    """Cast a computed value to the arrow type of its column.

    Vendor converters keep boolean-ish features (``*_valid``, ``*_repaired``);
    their stats columns are ``list<bool>`` while numeric stats are floats.
    """
    import pyarrow as pa

    if pa.types.is_list(typ) or pa.types.is_fixed_size_list(typ):
        if isinstance(value, (list, tuple)):
            return [_coerce(v, typ.value_type) for v in value]
        return value
    if pa.types.is_boolean(typ) and isinstance(value, (int, float, bool)):
        return bool(round(float(value)))
    return value


class TaskWriter:
    """One output dataset = one sub-task prompt = N segment-episodes."""

    def __init__(self, out_dir: Path, template: V3Dataset, task_text: str):
        self.out = Path(out_dir)
        self.template = template
        self.task = task_text
        self.data_schema = pq.read_schema(template.data_file(template.episodes[0]))
        self.episodes_schema = template.episodes_table.schema
        self.episode_rows: list[dict] = []
        self.stats_list: list[dict] = []
        self.frame_cursor = 0
        self.warnings: list[str] = []
        (self.out / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
        (self.out / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
        for key in template.video_keys:
            (self.out / "videos" / key / "chunk-000").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ add
    def add_episode(
        self,
        rows: pa.Table,
        videos: dict[str, Path],
        frames: dict[str, list[np.ndarray]] | None = None,
        offsets: dict[str, int] | None = None,
        source: dict | None = None,
    ) -> int:
        if rows.num_rows == 0:
            raise ValueError("empty segment")
        n = rows.num_rows
        ep = len(self.episode_rows)

        rows = self._rewrite_rows(rows, ep, n)
        data_file = self.out / DATA_PATH.format(chunk_index=0, file_index=ep)
        pq.write_table(rows, data_file)

        for key, src in videos.items():
            dst = self.out / VIDEO_PATH.format(video_key=key, chunk_index=0, file_index=ep)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        duration = n / self.template.fps
        row = self._base_episode_row()
        row.update(
            {
                "episode_index": ep,
                "tasks": [self.task],
                "length": n,
                "data/chunk_index": 0,
                "data/file_index": ep,
                "dataset_from_index": self.frame_cursor,
                "dataset_to_index": self.frame_cursor + n,
                "meta/episodes/chunk_index": 0,
                "meta/episodes/file_index": 0,
            }
        )
        offsets = offsets or {}
        for key in self.template.video_keys:
            off = offsets.get(key, 0) / self.template.fps
            row[f"videos/{key}/chunk_index"] = 0
            row[f"videos/{key}/file_index"] = ep
            row[f"videos/{key}/from_timestamp"] = round(off, 6)
            row[f"videos/{key}/to_timestamp"] = round(off + duration, 6)

        ep_stats = stats_mod.episode_stats(rows, self.template.features, frames)
        for feature, values in ep_stats.items():
            for name, value in values.items():
                col = f"stats/{feature}/{name}"
                if col in self.episodes_schema.names:
                    row[col] = stats_mod.to_python(value)

        self.episode_rows.append(row)
        self.stats_list.append(ep_stats)
        self.frame_cursor += n
        return ep

    # -------------------------------------------------------------- helpers
    def _rewrite_rows(self, rows: pa.Table, ep: int, n: int) -> pa.Table:
        fps = self.template.fps
        values = {
            "index": pa.array(range(self.frame_cursor, self.frame_cursor + n), pa.int64()),
            "episode_index": pa.array([ep] * n, pa.int64()),
            "frame_index": pa.array(range(n), pa.int64()),
            "task_index": pa.array([0] * n, pa.int64()),
            "timestamp": pa.array(np.arange(n, dtype=np.float32) / np.float32(fps), pa.float32()),
        }
        for name, arr in values.items():
            if name not in self.data_schema.names:
                continue
            i = self.data_schema.get_field_index(name)
            rows = rows.set_column(i, name, arr)
        return rows

    def _base_episode_row(self) -> dict:
        return {
            name: self.template.episodes_table.column(name)[0].as_py()
            for name in self.episodes_schema.names
        }

    # ------------------------------------------------------------- finalize
    def finalize(self) -> dict:
        n = len(self.episode_rows)
        if n == 0:
            raise ValueError(f"task {self.task!r}: no episodes to write")
        fields = list(zip(self.episodes_schema.names, self.episodes_schema.types))
        rows = [
            {name: _coerce(row.get(name), typ) for name, typ in fields}
            for row in self.episode_rows
        ]
        episodes = pa.Table.from_pylist(rows, schema=self.episodes_schema)
        pq.write_table(episodes, self.out / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

        import pandas as pd

        pd.DataFrame({"task_index": [0]}, index=pd.Index([self.task], name="task")).to_parquet(
            self.out / "meta" / "tasks.parquet"
        )

        info = json.loads(json.dumps(self.template.info))
        info["data_path"] = DATA_PATH
        info["video_path"] = VIDEO_PATH
        info["total_episodes"] = n
        info["total_frames"] = self.frame_cursor
        info["total_tasks"] = 1
        info["total_videos"] = n * len(self.template.video_keys)
        info["total_chunks"] = 1
        info["splits"] = {"train": f"0:{n}"}
        info.setdefault("chunks_size", 1000)
        (self.out / "meta" / "info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8"
        )

        agg = stats_mod.aggregate(self.stats_list)
        (self.out / "meta" / "stats.json").write_text(
            json.dumps({k: {kk: stats_mod.to_python(vv) for kk, vv in v.items()} for k, v in agg.items()}),
            encoding="utf-8",
        )
        return {
            "task": self.task,
            "path": str(self.out),
            "episodes": n,
            "frames": self.frame_cursor,
            "total_seconds": self.frame_cursor / self.template.fps,
        }
