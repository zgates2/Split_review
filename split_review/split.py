"""Split reviewed episodes into LeRobot v3 datasets, one directory per sub-task.

Boundaries are keyframe-snapped and shared by all cameras of an episode, so the
per-camera frame count always matches the number of data rows in the segment.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyarrow.compute as pc

from . import video
from .dataset import Episode, V3Dataset
from .reviewdb import ReviewDB
from .writer import TaskWriter

Progress = Callable[[str, float], None]


@dataclass
class Segment:
    seq: int
    start_frame: int
    end_frame: int
    prompt: str
    keep: bool

    @property
    def length(self) -> int:
        return max(0, self.end_frame - self.start_frame)


def _slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip())
    cleaned = cleaned.strip("._")[:40]
    return cleaned or fallback


def common_keyframes(ds: V3Dataset, episode: Episode) -> list[float]:
    """Union of every camera's keyframe times (episode-local seconds).

    Cameras can have wildly different GOPs (G2 head 15 frames, wrists 250), so a
    shared cut point only needs to exist on *some* stream: the per-camera cut is
    widened back to that camera's own keyframe and compensated with
    ``from_timestamp`` in the output dataset.
    """
    merged: set[float] = set()
    for key, ref in episode.videos.items():
        path = ds.video_file(ref)
        keys = video.keyframes(path)
        merged.update(
            round(k - ref.from_ts, 3) for k in keys if ref.from_ts - 1e-3 <= k <= ref.to_ts + 1e-3
        )
    merged.add(0.0)
    return sorted(merged)


def snap_marks(
    ds: V3Dataset,
    episode: Episode,
    marks: list[float],
    keys: list[float] | None = None,
    snap: bool = True,
):
    """Normalise mark times to frames; optionally snap them to a keyframe grid.

    Cutting does not require keyframes any more: a segment starts at the previous
    keyframe of each camera and the exact offset is stored in ``from_timestamp``.
    Snapping is therefore only a space optimisation, useful when the GOP is short.
    """
    duration = episode.length / ds.fps
    keys = keys if keys is not None else common_keyframes(ds, episode)
    snapped: list[float] = []
    detail = []
    for m in sorted(float(x) for x in marks):
        m = min(max(m, 0.0), duration)
        if not snap:
            m = round(m, 6)
            exact = abs(m * ds.fps - round(m * ds.fps)) < 1e-3
            if snapped and m <= snapped[-1] + 0.5 / ds.fps:
                detail.append({"requested": m, "snapped": snapped[-1], "exact": False, "dropped": True})
                continue
            snapped.append(m)
            detail.append({"requested": m, "snapped": m, "exact": exact, "dropped": False})
            continue
        if keys:
            best = min(keys, key=lambda k: abs(k - m))
            exact = abs(best - m) < 1e-3
        else:
            best, exact = m, True
        if snapped and best <= snapped[-1] + 0.5 / ds.fps:
            detail.append({"requested": m, "snapped": snapped[-1], "exact": False, "dropped": True})
            continue
        snapped.append(best)
        detail.append({"requested": m, "snapped": best, "exact": exact, "dropped": False})
    return snapped, detail


def build_segments(
    ds: V3Dataset,
    episode: Episode,
    marks: list[float],
    prompts: list[str],
    keeps: list[bool] | None = None,
    keys: list[float] | None = None,
    snap: bool = True,
) -> list[Segment]:
    """k marks -> k+1 segments; prompts[i]/keeps[i] apply to segment i."""
    snapped, _ = snap_marks(ds, episode, marks, keys, snap=snap)
    duration = episode.length / ds.fps
    edges = [0.0] + snapped + [duration]
    segs: list[Segment] = []
    for i in range(len(edges) - 1):
        a = ds.time_to_frame(edges[i])
        b = ds.time_to_frame(edges[i + 1])
        if b <= a:
            continue
        prompt = prompts[i] if i < len(prompts) and prompts[i] else ""
        keep = keeps[i] if keeps and i < len(keeps) else True
        segs.append(Segment(seq=i, start_frame=a, end_frame=b, prompt=prompt, keep=bool(keep) and bool(prompt)))
    return segs


def _dir_size(root: Path) -> int:
    total = 0
    stack = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class Exporter:
    """Export kept segments from one or more v3 datasets into by-task datasets.

    Multiple source datasets (a collection) are merged: every kept segment lands
    in the dataset of its sub-task prompt, regardless of which source it came
    from. All sources must share the same feature schema.
    """

    def __init__(self, sources: dict[str, V3Dataset], db: ReviewDB, out_root: Path):
        if not sources:
            raise ValueError("no source datasets")
        self.sources = dict(sources)
        self.db = db
        self.out_root = Path(out_root)
        self.template = next(iter(self.sources.values()))
        self.report: dict = {"segments": [], "tasks": [], "warnings": []}
        first = set(self.template.features)
        for key, ds in self.sources.items():
            if set(ds.features) != first:
                self.report["warnings"].append(
                    f"{ds.root.name}: feature set differs from {self.template.root.name}"
                )
            if abs(ds.fps - self.template.fps) > 1e-6:
                raise ValueError(f"{ds.root.name}: fps {ds.fps} != {self.template.fps}")

    def run(
        self,
        keys: list[str],
        name: str,
        progress: Progress | None = None,
        dry_run: bool = False,
        statuses: tuple[str, ...] = ("done", "reviewing"),
    ) -> dict:
        t0 = time.time()
        say = progress or (lambda msg, frac: None)
        template_prompts = self.db.template()

        jobs: list[tuple[str, str, dict, dict]] = []
        for key in keys:
            for st in self.db.episodes(key):
                if st["status"] not in statuses:
                    continue
                for seg in st.get("segments") or []:
                    if not seg.get("keep") or not seg.get("prompt"):
                        continue
                    jobs.append((seg["prompt"], key, st, seg))
        if not jobs:
            raise ValueError("no kept segments with a prompt; nothing to export")

        order: list[str] = [t for t in template_prompts if any(j[0] == t for j in jobs)]
        for prompt, _, _, _ in jobs:
            if prompt not in order:
                order.append(prompt)

        base = self.out_root / _slug(name, "split")
        out_dirs = {
            task: base / f"task_{i:02d}_{_slug(task, 'task')}" for i, task in enumerate(order, 1)
        }

        if dry_run:
            counts = {t: sum(1 for j in jobs if j[0] == t) for t in order}
            return {
                "dry_run": True,
                "tasks": counts,
                "total_segments": len(jobs),
                "sources": [str(self.sources[k].root) for k in keys],
            }

        self._preflight(base, jobs)
        base.mkdir(parents=True, exist_ok=True)
        tmp_root = Path(tempfile.mkdtemp(prefix=".cut_", dir=str(base)))
        writers = {t: TaskWriter(out_dirs[t], self.template, t) for t in order}
        report_segments = []
        try:
            for n, (prompt, key, st, seg) in enumerate(jobs, 1):
                ds = self.sources[key]
                ep = ds.episode(int(st["episode_index"]))
                label = f"{ds.root.name}/ep{ep.index}"
                say(
                    f"segment {n}/{len(jobs)}: {label} [{seg['start_frame']}:{seg['end_frame']}] -> {prompt}",
                    n / len(jobs) * 0.95,
                )
                frames = self._cut_segment(ds, ep, seg, tmp_root / f"{key}_e{ep.index}_s{seg['seq']}")
                rows = self._slice_rows(ds, ep, seg)
                expected = int(seg["end_frame"]) - int(seg["start_frame"])
                bad = {k: c for k, c in frames["counts"].items() if c != expected}
                if bad:
                    raise RuntimeError(
                        f"{label} segment {seg['seq']}: expected {expected} frames, "
                        f"got {bad} from {ep.videos}"
                    )
                out_ep = writers[prompt].add_episode(
                    rows,
                    frames["videos"],
                    frames["frames"],
                    offsets=frames["offsets"],
                    source={"dataset": key, "episode": ep.index, "segment": seg["seq"]},
                )
                report_segments.append(
                    {
                        "source_dataset": str(ds.root),
                        "source_episode": ep.index,
                        "segment": seg["seq"],
                        "prompt": prompt,
                        "start_frame": seg["start_frame"],
                        "end_frame": seg["end_frame"],
                        "frames": expected,
                        "seconds": expected / ds.fps,
                        "dataset": str(out_dirs[prompt]),
                        "episode_index": out_ep,
                        "video_offsets_frames": frames["offsets"],
                    }
                )
        except BaseException:
            # Never leave a directory that looks like a dataset but has no meta:
            # rename it so it cannot be mistaken for a trainable export.
            stamp = time.strftime("%Y%m%d_%H%M%S")
            failed = base.with_name(base.name + ".failed-" + stamp)
            try:
                base.rename(failed)
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        summaries = []
        for task, w in writers.items():
            if not w.episode_rows:
                shutil.rmtree(out_dirs[task], ignore_errors=True)
                continue
            summaries.append(w.finalize())
        self.report.update(
            {
                "name": name,
                "sources": [str(self.sources[k].root) for k in keys],
                "output": str(base),
                "mode": "by-task",
                "tasks": summaries,
                "segments": report_segments,
                "elapsed_s": round(time.time() - t0, 2),
            }
        )
        (base / "export_report.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.report

    # -------------------------------------------------------------- helpers
    def _preflight(self, base: Path, jobs: list) -> None:
        if base.exists() and any(base.iterdir()):
            raise ValueError(
                f"输出目录已存在且非空：{base}\n换一个目录名，或先把它删掉再导出。"
            )
        need = sum(_dir_size(ds.root) for ds in self.sources.values())
        need = int(need * 1.05)  # every segment may carry up to one extra GOP
        probe = base
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        self.report["space"] = {"estimated_bytes": need, "free_bytes": free, "probe": str(probe)}
        if free < need:
            raise ValueError(
                f"输出磁盘空间不足：这次导出预计需要约 {need / 1e9:.0f} GB，"
                f"{probe} 只剩 {free / 1e9:.0f} GB。\n"
                f"请把「输出到」换成空间更大的目录（例如 /home/...），或先清理磁盘。"
            )

    @staticmethod
    def _slice_rows(ds: V3Dataset, ep: Episode, seg: dict):
        rows = ds.episode_rows(ep)
        mask = pc.and_(
            pc.greater_equal(rows.column("frame_index"), int(seg["start_frame"])),
            pc.less(rows.column("frame_index"), int(seg["end_frame"])),
        )
        sliced = rows.filter(mask)
        if sliced.num_rows == 0:
            raise RuntimeError(f"episode {ep.index} segment {seg['seq']} has no rows")
        return sliced

    @staticmethod
    def _cut_segment(ds: V3Dataset, ep: Episode, seg: dict, tmp: Path) -> dict:
        tmp.mkdir(parents=True, exist_ok=True)
        start = int(seg["start_frame"]) / ds.fps
        end = int(seg["end_frame"]) / ds.fps
        videos, counts, frames, offsets = {}, {}, {}, {}
        for key, ref in ep.videos.items():
            src = ds.video_file(ref)
            dst = tmp / f"{_slug(key, 'cam')}.mp4"
            info = video.cut_to_boundary(src, ref.from_ts + start, ref.from_ts + end, ds.fps, dst)
            offsets[key] = info["offset_frames"]
            counts[key] = video.count_frames(dst) - info["offset_frames"]
            videos[key] = dst
            frames[key] = video.sample_frames(dst, max_samples=12)
        return {"videos": videos, "counts": counts, "frames": frames, "offsets": offsets}
