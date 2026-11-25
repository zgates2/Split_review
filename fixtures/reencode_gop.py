#!/usr/bin/env python3
"""Re-encode a fixture's videos with a sparse GOP, to exercise keyframe snapping.

Files keep the same frame count and fps; only the codec/GOP changes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from split_review.video import ffmpeg_exe


def main(src: str, dst: str, gop: int = 15, codec: str = "libx265") -> None:
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for mp4 in sorted(dst.rglob("*.mp4")):
        tmp = mp4.with_suffix(".tmp.mp4")
        cmd = [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(mp4), "-an", "-c:v", codec, "-pix_fmt", "yuv420p",
        ]
        if codec == "libx265":
            cmd += ["-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:log-level=error"]
        else:
            cmd += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        subprocess.run(cmd, check=True)
        tmp.replace(mp4)
        print(f"re-encoded {mp4.relative_to(dst)}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 15)
