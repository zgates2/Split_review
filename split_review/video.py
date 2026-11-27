"""Video helpers: probe keyframes, lossless cut, browser-friendly preview encode.

Keyframe snapping is the only cut mode: marks are moved to the nearest keyframe
of the source stream, so every exported segment is a plain ``-c copy`` of the
original bitstream (no re-encode, no quality loss).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import av

_KEYFRAME_CACHE: dict[str, list[float]] = {}


def ffmpeg_exe() -> str:
    explicit = os.environ.get("SPLIT_FFMPEG")
    if explicit and Path(explicit).is_file():
        return explicit
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # pragma: no cover - optional dependency
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg not found; set SPLIT_FFMPEG to an ffmpeg binary")


def probe(path: str | Path) -> dict:
    """Container/stream facts + all keyframe timestamps (seconds)."""
    path = str(path)
    cache_file = _cache_path(path)
    if cache_file and cache_file.is_file():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    keyframes: list[float] = []
    with av.open(path) as container:
        stream = container.streams.video[0]
        time_base = stream.time_base
        for packet in container.demux(stream):
            if packet.size and packet.is_keyframe and packet.pts is not None:
                keyframes.append(round(float(packet.pts * time_base), 6))
        info = {
            "codec": stream.codec_context.name,
            "width": stream.width,
            "height": stream.height,
            "fps": float(stream.average_rate) if stream.average_rate else 30.0,
            "frames": int(stream.frames or 0),
            "duration": float(stream.duration * time_base) if stream.duration else None,
            "keyframes": sorted(set(keyframes)),
        }
    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(info))
        except OSError:
            pass
    return info


def _cache_path(path: str) -> Path | None:
    root = os.environ.get("SPLIT_CACHE_DIR")
    if not root:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    digest = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    return Path(root) / "keyframes" / f"{digest}.json"


def keyframes(path: str | Path) -> list[float]:
    key = str(Path(path).resolve())
    if key not in _KEYFRAME_CACHE:
        _KEYFRAME_CACHE[key] = probe(path)["keyframes"]
    return _KEYFRAME_CACHE[key]


def snap_to_keyframe(t: float, keys: list[float], lo: float, hi: float) -> tuple[float, bool]:
    """Nearest keyframe inside [lo, hi]; returns (time, exact)."""
    if not keys:
        return t, True
    best = None
    for k in keys:
        if k < lo - 1e-6 or k > hi + 1e-6:
            continue
        if best is None or abs(k - t) < abs(best - t):
            best = k
    if best is None:
        best = min(max(t, lo), hi)
    return best, abs(best - t) < 1e-4


def keyframe_before(keys: list[float], t: float) -> float:
    """Largest keyframe <= t (0.0 when none)."""
    best = 0.0
    for k in keys:
        if k <= t + 1e-6:
            best = max(best, k)
        else:
            break
    return best


def cut_to_boundary(
    src: str | Path,
    start: float,
    end: float,
    fps: float,
    dst: str | Path,
) -> dict:
    """Lossless cut covering [start, end) starting at the previous keyframe.

    Cameras are encoded with different GOPs (the G2 wrist streams use a 250-frame
    GOP), so a cut can only start on a keyframe. We start early and report the
    offset; the dataset stores it as ``videos/<key>/from_timestamp`` so playback
    still begins exactly at the requested frame.
    """
    keys = keyframes(src)
    kf = keyframe_before(keys, start)
    offset_frames = int(round((start - kf) * fps))
    total_frames = offset_frames + int(round((end - start) * fps))
    cut(src, kf, end, dst, frames=total_frames)
    return {"offset_frames": offset_frames, "frames": total_frames, "keyframe": kf}


def cut(src: str | Path, start: float, end: float, dst: str | Path, frames: int | None = None) -> float:
    """Lossless stream copy of [start, end) seconds.

    ``frames`` pins the exact packet count: with ``-c copy`` the time-based
    ``-t`` bound can overshoot by a couple of frames around B-frames, which
    would break the row/video 1:1 contract.
    """
    start = max(0.0, float(start))
    duration = max(0.0, float(end) - start)
    if duration <= 0:
        raise ValueError(f"empty cut range: {start}..{end}")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.6f}",
            "-i", str(src),
            *(("-frames:v", str(int(frames))) if frames else ("-t", f"{duration:.6f}")),
            "-map", "0:v:0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(dst),
        ]
    )
    return duration


def make_preview(src: str | Path, start: float, end: float, dst: str | Path, max_height: int = 720) -> None:
    """H.264 preview for the browser (the export never uses this)."""
    start = max(0.0, float(start))
    duration = max(0.0, float(end) - start)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale=-2:{max_height}"
    _run(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.6f}",
            "-i", str(src),
            "-t", f"{duration:.6f}",
            "-map", "0:v:0",
            "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def count_frames(path: str | Path) -> int:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if stream.frames:
            return int(stream.frames)
        return sum(1 for p in container.demux(stream) if p.size)


def sample_frames(path: str | Path, max_samples: int = 12, window: int = 4) -> list:
    """Decode a few frames near the start as HxWx3 uint8 arrays (image stats).

    Image statistics are only used for normalisation/diagnostics, so they do not
    need frames spread over the whole segment. Seeking across a long-GOP file
    forced a near-complete decode (~6 s per camera per segment); decoding the
    first ``max_samples * window`` frames and taking every ``window``-th one
    gives the same order of magnitude in about a tenth of the time.
    """
    import numpy as np  # noqa: F401  (kept for parity with the previous version)

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        keep = max(1, int(window))
        limit = max(1, int(max_samples)) * keep
        frames = []
        for i, frame in enumerate(container.decode(stream)):
            if i % keep == 0:
                frames.append(frame.to_ndarray(format="rgb24"))
                if len(frames) >= max_samples:
                    break
            if i >= limit:
                break
        return frames


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}"
        )
