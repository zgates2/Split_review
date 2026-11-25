"""H.264 preview cache with background pre-generation.

Switching episodes in the review UI is the slow part: a 1–2 minute G2 episode
takes several seconds per camera to transcode. While the operator watches
episode k we can already build k+1 and k+2, so the next switch is instant.

The cache is keyed by (dataset, episode, camera) and invalidated by the source
file's mtime. A worker pool (3 threads by default) does the transcoding; the
HTTP request path shares the same per-file lock, so a request that arrives while
a prefetch is running just waits for that file instead of encoding it twice.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from . import video


class PreviewCache:
    def __init__(self, resolve, root, workers: int = 3, max_queue: int = 48):
        """``resolve(dataset_key, episode_index) -> (dataset, episode)``."""
        self.resolve = resolve
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._pending: set[tuple[str, int, str]] = set()
        self._guard = threading.Lock()
        self._file_locks: dict[str, threading.Lock] = {}
        self.done = 0
        self.failed = 0
        self.dropped = 0
        for i in range(max(1, workers)):
            threading.Thread(target=self._work, name=f"preview-{i}", daemon=True).start()

    # ------------------------------------------------------------------ paths
    def path_for(self, key: str, index: int, camera: str) -> Path:
        return self.root / key / f"ep{index:04d}_{camera.replace('.', '_')}.mp4"

    def _file_lock(self, path: Path) -> threading.Lock:
        with self._guard:
            return self._file_locks.setdefault(str(path), threading.Lock())

    # ----------------------------------------------------------------- ensure
    def ensure(self, key: str, index: int, camera: str) -> Path:
        """Return a fresh preview, transcoding it if needed (blocking)."""
        ds, ep = self.resolve(key, index)
        if camera not in ep.videos:
            raise KeyError(f"no camera {camera}")
        ref = ep.videos[camera]
        src = ds.video_file(ref)
        out = self.path_for(key, index, camera)
        with self._file_lock(out):
            if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.mp4")
            video.make_preview(src, ref.from_ts, ref.to_ts, tmp)
            tmp.replace(out)
        return out

    # ---------------------------------------------------------------- prefetch
    def enqueue_episode(self, key: str, index: int) -> int:
        """Queue every camera of one episode that is not cached yet."""
        try:
            ds, ep = self.resolve(key, index)
        except Exception:
            return 0
        queued = 0
        for camera, ref in ep.videos.items():
            out = self.path_for(key, index, camera)
            src = ds.video_file(ref)
            if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
                continue
            item = (key, int(index), camera)
            with self._guard:
                if item in self._pending:
                    continue
                self._pending.add(item)
            try:
                self._q.put_nowait(item)
                queued += 1
            except queue.Full:
                with self._guard:
                    self._pending.discard(item)
                self.dropped += 1
        return queued

    def _work(self) -> None:
        while True:
            key, index, camera = self._q.get()
            try:
                self.ensure(key, index, camera)
                self.done += 1
            except Exception:  # noqa: BLE001 - a bad episode must not kill the worker
                self.failed += 1
            finally:
                with self._guard:
                    self._pending.discard((key, index, camera))
                self._q.task_done()

    def status(self) -> dict:
        return {
            "queued": self._q.qsize(),
            "pending": len(self._pending),
            "done": self.done,
            "failed": self.failed,
            "dropped": self.dropped,
        }
