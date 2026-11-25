"""FastAPI review service: dataset browse, video preview, marks, export."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .dataset import DatasetError, V3Dataset
from .reviewdb import ReviewDB
from .split import Exporter, build_segments, common_keyframes, snap_marks
from . import video

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "static"

_CACHE: dict[str, tuple[float, V3Dataset]] = {}
_JOBS: dict[int, threading.Thread] = {}


def _key(root: Path) -> str:
    import hashlib

    return hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]


def _open(path: str | Path) -> V3Dataset:
    root = Path(path).expanduser().resolve()
    stamp = (root / "meta" / "info.json").stat().st_mtime
    cached = _CACHE.get(str(root))
    if cached and cached[0] == stamp:
        return cached[1]
    ds = V3Dataset(root)
    _CACHE[str(root)] = (stamp, ds)
    return ds


def _range_response(path: Path, request: Request, media_type: str = "video/mp4"):
    size = path.stat().st_size
    header = request.headers.get("range")
    if not header:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    try:
        unit, spec = header.split("=", 1)
        start_s, _, end_s = spec.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except Exception:
        raise HTTPException(status_code=416, detail="bad Range header")
    if start >= size:
        raise HTTPException(status_code=416, detail="range out of bounds", headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    length = end - start + 1

    def iterator():
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


def create_app(db_path: str = "state/review.db", media_root: str | None = None) -> FastAPI:
    db = ReviewDB(db_path)
    preview_root = Path(media_root) if media_root else Path(db_path).parent / "preview"
    preview_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="G2 Split Review")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # ------------------------------------------------------------------ ui
    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    # ------------------------------------------------------------ datasets
    @app.get("/api/datasets")
    def list_datasets():
        out = []
        for d in db.list_datasets():
            out.append({**d, "review": db.stats(d["key"])})
        return out

    @app.post("/api/datasets")
    def add_dataset(body: dict = Body(...)):
        path = body.get("path")
        if not path:
            raise HTTPException(400, "path required")
        try:
            ds = _open(path)
        except DatasetError as exc:
            raise HTTPException(400, str(exc))
        key = _key(ds.root)
        db.register_dataset(key, str(ds.root), ds.root.name, ds.fps, ds.n_episodes,
                            [e.length for e in ds.episodes])
        return {"key": key, "summary": ds.summary()}

    @app.get("/api/datasets/{key}")
    def dataset(key: str):
        row = db.get_dataset(key)
        if not row:
            raise HTTPException(404, "unknown dataset")
        ds = _open(row["path"])
        return {**row, "summary": ds.summary(), "review": db.stats(key)}

    @app.get("/api/datasets/{key}/episodes")
    def episodes(key: str, status: str | None = None):
        row = db.get_dataset(key)
        if not row:
            raise HTTPException(404, "unknown dataset")
        ds = _open(row["path"])
        items = []
        by_index = {e.index: e for e in ds.episodes}
        for st in db.episodes(key):
            ep = by_index.get(st["episode_index"])
            if not ep:
                continue
            item = {
                "episode_index": st["episode_index"],
                "length": ep.length,
                "duration_s": round(ep.length / ds.fps, 3),
                "status": st["status"],
                "tasks": ep.tasks,
                "n_segments": len(st.get("segments") or []),
                "kept": sum(1 for s in (st.get("segments") or []) if s.get("keep")),
                "updated_at": st.get("updated_at"),
            }
            if status and item["status"] != status:
                continue
            items.append(item)
        return {"dataset": row, "episodes": items, "review": db.stats(key)}

    @app.get("/api/datasets/{key}/episodes/{index}")
    def episode(key: str, index: int):
        row = db.get_dataset(key)
        if not row:
            raise HTTPException(404, "unknown dataset")
        ds = _open(row["path"])
        try:
            ep = ds.episode(index)
        except DatasetError as exc:
            raise HTTPException(404, str(exc))
        st = db.episode(key, index) or {"marks": [], "segments": [], "status": "new", "note": ""}
        payload = _episode_payload(ds, ep, st)
        payload["template"] = db.template()
        return {**payload, "dataset_key": key, "dataset_name": row["name"]}

    @app.put("/api/datasets/{key}/episodes/{index}")
    def save_episode(key: str, index: int, body: dict = Body(...)):
        row = db.get_dataset(key)
        if not row:
            raise HTTPException(404, "unknown dataset")
        ds = _open(row["path"])
        ep = ds.episode(index)
        marks = [float(m) for m in body.get("marks") or []]
        keys = common_keyframes(ds, ep)
        use_snap = bool(body.get("snap", True))
        snapped, detail = snap_marks(ds, ep, marks, keys, snap=use_snap)
        prompts = body.get("prompts") or []
        keeps = body.get("keeps") or []
        segments = build_segments(ds, ep, marks, prompts, keeps, keys, snap=use_snap)
        payload = [
            {
                "seq": s.seq,
                "start_frame": s.start_frame,
                "end_frame": s.end_frame,
                "start": round(s.start_frame / ds.fps, 3),
                "end": round(s.end_frame / ds.fps, 3),
                "prompt": s.prompt,
                "keep": s.keep,
            }
            for s in segments
        ]
        db.save_episode_state(key, index, marks, payload, body.get("status") or "done", body.get("note", ""))
        return {"marks": marks, "snapped": snapped, "snap_detail": detail, "segments": payload}

    # -------------------------------------------------------------- videos
    @app.get("/api/media/{key}/{index}")
    def media(key: str, index: int, camera: str, request: Request):
        row = db.get_dataset(key)
        if not row:
            raise HTTPException(404, "unknown dataset")
        ds = _open(row["path"])
        ep = ds.episode(index)
        if camera not in ep.videos:
            raise HTTPException(404, f"no camera {camera}")
        ref = ep.videos[camera]
        src = ds.video_file(ref)
        out = preview_root / key / f"ep{index:04d}_{camera.replace('.', '_')}.mp4"
        if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.mp4")
            video.make_preview(src, ref.from_ts, ref.to_ts, tmp)
            tmp.replace(out)
        return _range_response(out, request)

    # --------------------------------------------------------- collections
    @app.get("/api/collections")
    def list_collections():
        out = []
        for c in db.list_collections():
            agg = {"total": 0, "done": 0, "skipped": 0}
            for it in db.collection_items(c["key"]):
                st = db.episode(it["dataset_key"], it["episode_index"])
                if not st:
                    continue
                agg["total"] += 1
                if st["status"] in agg:
                    agg[st["status"]] += 1
            out.append({**c, "review": agg})
        return out

    @app.post("/api/collections")
    def add_collection(body: dict = Body(...)):
        path = body.get("path")
        if not path:
            raise HTTPException(400, "path required")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(400, f"not a directory: {root}")
        children = sorted(
            [p for p in root.iterdir() if (p / "meta" / "info.json").is_file()],
            key=lambda p: p.name,
        )
        if not children:
            raise HTTPException(400, f"no LeRobot v3 dataset (meta/info.json) under {root}")
        members: list[tuple[str, int]] = []
        skipped = []
        for child in children:
            try:
                ds = _open(child)
            except DatasetError as exc:
                skipped.append({"path": str(child), "reason": str(exc)})
                continue
            key = _key(ds.root)
            db.register_dataset(key, str(ds.root), ds.root.name, ds.fps, ds.n_episodes,
                                [e.length for e in ds.episodes])
            for ep in ds.episodes:
                members.append((key, ep.index))
        ckey = "col-" + hashlib.sha1(str(root).encode()).hexdigest()[:10]
        db.create_collection(ckey, str(root), root.name, members)
        return {"key": ckey, "name": root.name, "datasets": len(children) - len(skipped),
                "episodes": len(members), "skipped": skipped[:10], "skipped_count": len(skipped)}

    @app.get("/api/collections/{ckey}/items")
    def collection_items(ckey: str):
        col = db.get_collection(ckey)
        if not col:
            raise HTTPException(404, "unknown collection")
        out = []
        for ord_, it in enumerate(db.collection_items(ckey)):
            row = db.get_dataset(it["dataset_key"])
            if not row:
                continue
            ds = _open(row["path"])
            try:
                ep = ds.episode(it["episode_index"])
            except DatasetError:
                continue
            st = db.episode(it["dataset_key"], it["episode_index"]) or {}
            out.append({
                "ord": ord_,
                "dataset_key": it["dataset_key"],
                "dataset_name": row["name"],
                "episode_index": it["episode_index"],
                "length": ep.length,
                "duration_s": round(ep.length / ds.fps, 3),
                "status": st.get("status", "new"),
                "n_segments": len(st.get("segments") or []),
                "kept": sum(1 for x in (st.get("segments") or []) if x.get("keep")),
                "updated_at": st.get("updated_at"),
            })
        agg = {"total": len(out)}
        for item in out:
            agg[item["status"]] = agg.get(item["status"], 0) + 1
        return {"collection": col, "items": out, "review": agg}

    @app.get("/api/collections/{ckey}/items/{ord_}")
    def collection_item(ckey: str, ord_: int):
        col = db.get_collection(ckey)
        if not col:
            raise HTTPException(404, "unknown collection")
        items = db.collection_items(ckey)
        if ord_ < 0 or ord_ >= len(items):
            raise HTTPException(404, "item out of range")
        it = items[ord_]
        row = db.get_dataset(it["dataset_key"])
        if not row:
            raise HTTPException(404, "member dataset missing")
        ds = _open(row["path"])
        ep = ds.episode(it["episode_index"])
        st = db.episode(it["dataset_key"], it["episode_index"]) or {
            "marks": [], "segments": [], "status": "new", "note": ""}
        payload = _episode_payload(ds, ep, st)
        payload["template"] = db.template()
        return {
            **payload,
            "dataset_key": it["dataset_key"],
            "dataset_name": row["name"],
            "item_ord": ord_,
            "collection_key": ckey,
        }

    # ------------------------------------------------------------ template
    @app.get("/api/template")
    def get_template():
        return {"prompts": db.template()}

    @app.put("/api/template")
    def put_template(body: dict = Body(...)):
        prompts = [str(p).strip() for p in body.get("prompts") or [] if str(p).strip()]
        db.set_template(prompts)
        return {"prompts": prompts}

    # -------------------------------------------------------------- export
    @app.post("/api/export")
    def start_export(body: dict = Body(...)):
        key = body.get("dataset") or body.get("collection")
        name = body.get("name") or time.strftime("split_%Y%m%d_%H%M%S")
        out_root = body.get("out") or "/data/g2/lerobot_v2.1"
        output = str(Path(out_root) / name)
        eid = db.create_export(name, key, output)

        def work():
            try:
                keys = [key]
                if key and db.get_collection(key):
                    keys = sorted({it["dataset_key"] for it in db.collection_items(key)})
                sources = {}
                for k in keys:
                    row = db.get_dataset(k)
                    if row:
                        sources[k] = _open(row["path"])
                exp = Exporter(sources, db, Path(out_root))
                report = exp.run(keys, name, progress=lambda m, f: db.update_export(eid, progress=f"{f:.2f}|{m}"))
                db.update_export(eid, status="done", progress="1.0|完成", report=report)
            except Exception as exc:  # noqa: BLE001
                db.update_export(eid, status="failed", progress=f"error|{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=work, name=f"export-{eid}", daemon=True)
        _JOBS[eid] = thread
        thread.start()
        return {"id": eid, "output": output}

    @app.get("/api/export/{eid}")
    def export_status(eid: int):
        job = db.get_export(eid)
        if not job:
            raise HTTPException(404, "unknown export")
        frac, _, msg = (job.get("progress") or "").partition("|")
        job["progress_frac"] = float(frac) if frac else 0.0
        job["progress_msg"] = msg
        return job

    @app.get("/api/exports")
    def exports(dataset: str | None = None):
        return {"exports": db.list_exports(dataset)}

    @app.exception_handler(DatasetError)
    def dataset_error(_request, exc):  # pragma: no cover
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


def _episode_payload(ds: V3Dataset, ep, st: dict) -> dict:
    keys = common_keyframes(ds, ep)
    duration = ep.length / ds.fps
    median_gop = (duration / max(1, len(keys) - 1)) if len(keys) > 1 else duration
    marks = st.get("marks") or []
    snapped, detail = snap_marks(ds, ep, marks, keys) if marks else ([], [])
    return {
        "episode_index": ep.index,
        "length": ep.length,
        "duration_s": round(ep.length / ds.fps, 3),
        "fps": ds.fps,
        "tasks": ep.tasks,
        "cameras": ds.video_keys,
        "video_meta": {k: {"from": v.from_ts, "to": v.to_ts} for k, v in ep.videos.items()},
        "keyframes": [round(k, 3) for k in keys if k <= ep.length / ds.fps + 1e-6],
        "marks": marks,
        "snapped": snapped,
        "snap_detail": detail,
        "segments": st.get("segments") or [],
        "status": st.get("status", "new"),
        "note": st.get("note", ""),
        "template": None,
        "snap_default": median_gop <= 1.0,
        "median_keyframe_gap_s": round(median_gop, 3),
    }
