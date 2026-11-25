"""Command line entry point: info / mark / export / verify / audit / serve."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .dataset import V3Dataset
from .reviewdb import ReviewDB
from .split import Exporter, build_segments, common_keyframes, snap_marks


def dataset_key(root: Path) -> str:
    return hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]


def cmd_info(a) -> int:
    ds = V3Dataset(a.dataset)
    print(json.dumps(ds.summary(), ensure_ascii=False, indent=2))
    return 0


def cmd_keyframes(a) -> int:
    ds = V3Dataset(a.dataset)
    ep = ds.episode(a.episode)
    keys = common_keyframes(ds, ep)
    print(json.dumps({"episode": ep.index, "duration_s": ep.length / ds.fps,
                      "keyframes_s": keys[:80], "count": len(keys)}, ensure_ascii=False, indent=2))
    return 0


def cmd_mark(a) -> int:
    ds = V3Dataset(a.dataset)
    db = ReviewDB(a.db)
    key = dataset_key(ds.root)
    db.register_dataset(key, str(ds.root), ds.root.name, ds.fps, ds.n_episodes,
                        [e.length for e in ds.episodes])
    if not db.template():
        db.set_template(a.prompts)
    ep = ds.episode(a.episode)
    marks = [float(x) for x in a.marks.split(",") if x.strip()]
    prompts = a.prompts if len(a.prompts) >= len(marks) + 1 else (db.template() + [""] * 10)
    segs = build_segments(ds, ep, marks, prompts[: len(marks) + 1])
    db.save_episode_state(key, ep.index, marks,
                          [dict(seq=s.seq, start_frame=s.start_frame, end_frame=s.end_frame,
                                start=round(s.start_frame / ds.fps, 3),
                                end=round(s.end_frame / ds.fps, 3),
                                prompt=s.prompt, keep=s.keep) for s in segs],
                          "done")
    print(json.dumps({"episode": ep.index, "marks": marks,
                      "segments": [(s.start_frame, s.end_frame, s.prompt) for s in segs]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_scan(a) -> int:
    """Register every LeRobot v3 dataset under a directory as one collection."""
    root = Path(a.path).expanduser().resolve()
    children = sorted([p for p in root.iterdir() if (p / "meta" / "info.json").is_file()],
                      key=lambda p: p.name) if root.is_dir() else []
    if not children:
        print(f"no v3 dataset under {root}", file=sys.stderr)
        return 1
    db = ReviewDB(a.db)
    members = []
    for child in children:
        ds = V3Dataset(child)
        key = dataset_key(ds.root)
        db.register_dataset(key, str(ds.root), ds.root.name, ds.fps, ds.n_episodes,
                            [e.length for e in ds.episodes])
        members.extend((key, e.index) for e in ds.episodes)
    ckey = "col-" + hashlib.sha1(str(root).encode()).hexdigest()[:10]
    db.create_collection(ckey, str(root), root.name, members)
    print(json.dumps({"collection": ckey, "name": root.name, "datasets": len(children),
                      "episodes": len(members)}, ensure_ascii=False, indent=2))
    return 0


def cmd_export(a) -> int:
    db = ReviewDB(a.db)
    keys = []
    if a.collection:
        items = db.collection_items(a.collection)
        keys = sorted({it["dataset_key"] for it in items})
    else:
        ds0 = V3Dataset(a.dataset)
        keys = [dataset_key(ds0.root)]
    if not keys:
        print("nothing to export", file=sys.stderr)
        return 1
    sources = {}
    for k in keys:
        row = db.get_dataset(k)
        if row:
            sources[k] = V3Dataset(row["path"])
    if not sources:
        print("dataset not registered; run `mark` or `scan` first", file=sys.stderr)
        return 1
    exp = Exporter(sources, db, Path(a.out))
    report = exp.run(keys, a.name, dry_run=a.dry_run,
                     progress=lambda m, f: print(f"[{f*100:5.1f}%] {m}", file=sys.stderr))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(a) -> int:
    from .verify import verify

    result = verify(Path(a.dataset), sample=not a.no_sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_audit(a) -> int:
    from .audit import audit

    print(json.dumps(audit(Path(a.dataset)), ensure_ascii=False, indent=2))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    from .server import create_app

    app = create_app(db_path=a.db, media_root=a.media_root)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="split_review", description=__doc__)
    p.add_argument("--db", default="state/review.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info"); s.add_argument("--dataset", required=True); s.set_defaults(func=cmd_info)
    s = sub.add_parser("keyframes"); s.add_argument("--dataset", required=True)
    s.add_argument("--episode", type=int, default=0); s.set_defaults(func=cmd_keyframes)
    s = sub.add_parser("mark"); s.add_argument("--dataset", required=True)
    s.add_argument("--episode", type=int, default=0); s.add_argument("--marks", required=True)
    s.add_argument("--prompts", nargs="+", default=["拿起手机", "放进盒子", "合上盒盖"])
    s.set_defaults(func=cmd_mark)
    s = sub.add_parser("scan"); s.add_argument("--path", required=True); s.set_defaults(func=cmd_scan)
    s = sub.add_parser("export")
    s.add_argument("--dataset"); s.add_argument("--collection")
    s.add_argument("--name", required=True); s.add_argument("--out", required=True)
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(func=cmd_export)
    s = sub.add_parser("verify"); s.add_argument("--dataset", required=True)
    s.add_argument("--no-sample", action="store_true"); s.set_defaults(func=cmd_verify)
    s = sub.add_parser("audit"); s.add_argument("--dataset", required=True); s.set_defaults(func=cmd_audit)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=18770)
    s.add_argument("--media-root", default=None); s.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
