"""Contract tests: official writer -> split -> official loader.

Run with:  python -m pytest tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from split_review.dataset import V3Dataset  # noqa: E402
from split_review.reviewdb import ReviewDB  # noqa: E402
from split_review.split import Exporter, build_segments, snap_marks  # noqa: E402
from split_review.verify import verify  # noqa: E402

CAMS = ("head_color", "hand_left_color")
H, W, FPS = 64, 96, 30


def _make_dataset(root: Path, episodes: int = 2, frames: int = 60) -> Path:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    features = {
        "observation.state": {"dtype": "float32", "shape": (4,), "names": ["a", "b", "c", "d"]},
        "action": {"dtype": "float32", "shape": (4,), "names": ["a", "b", "c", "d"]},
    }
    for cam in CAMS:
        features[f"observation.images.{cam}"] = {
            "dtype": "video", "shape": (H, W, 3), "names": ["height", "width", "channel"],
        }
    ds = LeRobotDataset.create(repo_id="local/contract", fps=FPS, features=features, root=root)
    g = 0
    for _ in range(episodes):
        for _ in range(frames):
            img = np.zeros((H, W, 3), dtype=np.uint8)
            img[:, (g * 2) % W:(g * 2) % W + 4] = 255
            frame = {
                "observation.state": np.full(4, g / 100.0, dtype=np.float32),
                "action": np.full(4, -g / 100.0, dtype=np.float32),
                "task": "long horizon task",
            }
            for cam in CAMS:
                frame[f"observation.images.{cam}"] = img.copy()
            ds.add_frame(frame)
            g += 1
        ds.save_episode()
    ds.finalize()
    return root


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("fixture") / "src"
    _make_dataset(root)
    return root


def test_snap_picks_nearest_keyframe():
    keys = [0.0, 0.5, 1.0, 1.5, 2.0]
    ds = type("D", (), {"fps": 30.0})()
    ep = type("E", (), {"index": 0, "length": 60})()
    snapped, detail = snap_marks(ds, ep, [0.53, 1.44], keys)
    assert snapped == [0.5, 1.5]
    assert all(not d["exact"] for d in detail)


def test_segments_from_marks():
    ds = type("D", (), {"fps": 30.0, "time_to_frame": lambda self, t: int(round(t * 30))})()
    ep = type("E", (), {"index": 0, "length": 120})()
    segs = build_segments(ds, ep, [1.0, 2.0], ["a", "b", "c"], [True, False, True], [1.0, 2.0])
    assert [(s.start_frame, s.end_frame) for s in segs] == [(0, 30), (30, 60), (60, 120)]
    assert [s.keep for s in segs] == [True, False, True]


def test_split_produces_loadable_task_datasets(fixture_root, tmp_path):
    ds = V3Dataset(fixture_root)
    db = ReviewDB(tmp_path / "review.db")
    db.register_dataset("k", str(fixture_root), "src", ds.fps, ds.n_episodes,
                        [e.length for e in ds.episodes])
    db.set_template(["拿起", "放下", "收尾"])
    ep = ds.episode(0)
    marks = [1.0, 1.5]
    segs = build_segments(ds, ep, marks, ["拿起", "放下", "收尾"])
    db.save_episode_state(
        "k", 0, marks,
        [dict(seq=s.seq, start_frame=s.start_frame, end_frame=s.end_frame,
              start=s.start_frame / ds.fps, end=s.end_frame / ds.fps,
              prompt=s.prompt, keep=True) for s in segs],
        "done",
    )

    out = tmp_path / "out"
    report = Exporter({"k": ds}, db, out).run(["k"], "split_test")
    tasks = {t["task"]: t for t in report["tasks"]}
    assert set(tasks) == {"拿起", "放下", "收尾"}
    # boundaries are keyframe-snapped: the fixture's GOP is 2 frames, so assert
    # consistency with the snapped segment plan rather than hard-coded numbers.
    expected = {s.prompt: s.end_frame - s.start_frame for s in segs}
    assert {k: v["frames"] for k, v in tasks.items()} == expected
    assert sum(t["frames"] for t in report["tasks"]) == ds.episode(0).length

    for meta in report["tasks"]:
        result = verify(Path(meta["path"]), sample=True)
        assert result["ok"], json.dumps(result, ensure_ascii=False)[:800]
        assert result["loader"]["episodes"] == 1

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds2 = LeRobotDataset(repo_id="local/check", root=tasks["拿起"]["path"])
    frame = ds2[0]
    assert frame["task"] == "拿起"
    assert frame["frame_index"] == 0
    assert float(frame["timestamp"]) == 0.0
