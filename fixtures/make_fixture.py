#!/usr/bin/env python3
"""Generate a synthetic LeRobot v3.0 dataset with the OFFICIAL writer.

This is the "golden schema" for the split tool: whatever the official
`LeRobotDataset.create` emits is what we must read and reproduce.

Usage:
    python fixtures/make_fixture.py --out /tmp/fx --episodes 2 --frames 150
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

STATE_DIM = 24
HEIGHT, WIDTH = 180, 320
FPS = 30
CAMS = ("head_color", "hand_left_color", "hand_right_color")
NAMES = (
    [f"idx0{i}_body_joint{i}" for i in range(1, 6)]
    + [f"idx{10 + i}_head_joint{i}" for i in range(1, 4)]
    + [f"idx{20 + i}_arm_l_joint{i}" for i in range(1, 8)]
    + ["idx31_gripper_l_inner_joint1"]
    + [f"idx{60 + i}_arm_r_joint{i}" for i in range(1, 8)]
    + ["idx71_gripper_r_inner_joint1"]
)


def features() -> dict:
    f = {
        "observation.state": {"dtype": "float32", "shape": (STATE_DIM,), "names": NAMES},
        "action": {"dtype": "float32", "shape": (STATE_DIM,), "names": NAMES},
    }
    for cam in CAMS:
        f[f"observation.images.{cam}"] = {
            "dtype": "video",
            "shape": (HEIGHT, WIDTH, 3),
            "names": ["height", "width", "channel"],
        }
    return f


def frame_image(global_frame: int, cam_index: int) -> np.ndarray:
    """Deterministic, visually unambiguous pattern: moving bar + per-camera tint."""
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    x = (global_frame * 3) % WIDTH
    img[:, :, :] = 40 + 20 * cam_index
    img[:, max(0, x - 20): min(WIDTH, x + 20), cam_index] = 255
    # encode frame index as a block of bright pixels (row 0..7, 8 bits)
    for bit in range(9):
        if global_frame & (1 << bit):
            img[10:20, 10 + bit * 12: 18 + bit * 12, :] = 255
    return img


def build(out: Path, episodes: int, frames: int) -> None:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"output {out} is not empty; refusing to overwrite")

    ds = LeRobotDataset.create(
        repo_id="local/fixture_v3",
        fps=FPS,
        features=features(),
        root=out,
        use_videos=True,
    )

    g = 0
    for ep in range(episodes):
        for i in range(frames):
            t = g / FPS
            state = np.asarray(
                [np.sin(t * (0.5 + k * 0.1) + k) * (0.3 + 0.02 * k) for k in range(STATE_DIM)],
                dtype=np.float32,
            )
            action = np.asarray(
                [np.sin((t + 0.1) * (0.5 + k * 0.1) + k) * (0.3 + 0.02 * k) for k in range(STATE_DIM)],
                dtype=np.float32,
            )
            frame = {"observation.state": state, "action": action, "task": "pack the phone boxes"}
            for ci, cam in enumerate(CAMS):
                frame[f"observation.images.{cam}"] = frame_image(g, ci)
            ds.add_frame(frame)
            g += 1
        ds.save_episode()
        print(f"episode {ep} saved ({frames} frames)", file=sys.stderr)

    ds.finalize()
    print(f"fixture written to {out}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--episodes", type=int, default=2)
    ap.add_argument("--frames", type=int, default=150)
    a = ap.parse_args()
    build(Path(a.out).expanduser().resolve(), a.episodes, a.frames)


if __name__ == "__main__":
    main()
