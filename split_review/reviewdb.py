"""SQLite review state: datasets, per-episode marks/segments, prompt template, exports."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset(
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    name TEXT,
    fps REAL,
    n_episodes INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS episode(
    dataset_key TEXT NOT NULL,
    episode_index INTEGER NOT NULL,
    length INTEGER,
    status TEXT DEFAULT 'new',
    marks TEXT DEFAULT '[]',
    segments TEXT DEFAULT '[]',
    note TEXT DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY(dataset_key, episode_index)
);
CREATE TABLE IF NOT EXISTS template(
    ord INTEGER PRIMARY KEY,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection(
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    name TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS collection_item(
    collection_key TEXT NOT NULL,
    ord INTEGER NOT NULL,
    dataset_key TEXT NOT NULL,
    episode_index INTEGER NOT NULL,
    PRIMARY KEY(collection_key, ord)
);
CREATE TABLE IF NOT EXISTS export(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    dataset_key TEXT,
    output TEXT,
    status TEXT,
    progress TEXT,
    report TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ datasets
    def register_dataset(self, key: str, path: str, name: str, fps: float, n_episodes: int, lengths: list[int]):
        with self._lock:
            self._conn.execute(
                "INSERT INTO dataset(key,path,name,fps,n_episodes,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET path=excluded.path, name=excluded.name, "
                "fps=excluded.fps, n_episodes=excluded.n_episodes",
                (key, path, name, fps, n_episodes, _now()),
            )
            for i, length in enumerate(lengths):
                self._conn.execute(
                    "INSERT INTO episode(dataset_key,episode_index,length,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(dataset_key,episode_index) DO UPDATE SET length=excluded.length",
                    (key, i, int(length), _now()),
                )
            self._conn.commit()

    def list_datasets(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM dataset ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get_dataset(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dataset WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ episodes
    def episodes(self, key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episode WHERE dataset_key=? ORDER BY episode_index", (key,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["marks"] = json.loads(d.get("marks") or "[]")
            d["segments"] = json.loads(d.get("segments") or "[]")
            out.append(d)
        return out

    def episode(self, key: str, index: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM episode WHERE dataset_key=? AND episode_index=?", (key, index)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["marks"] = json.loads(d.get("marks") or "[]")
        d["segments"] = json.loads(d.get("segments") or "[]")
        return d

    def save_episode_state(
        self, key: str, index: int, marks: list, segments: list, status: str, note: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE episode SET marks=?, segments=?, status=?, note=?, updated_at=? "
                "WHERE dataset_key=? AND episode_index=?",
                (json.dumps(marks), json.dumps(segments), status, note, _now(), key, index),
            )
            self._conn.commit()

    def stats(self, key: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c FROM episode WHERE dataset_key=? GROUP BY status", (key,)
            ).fetchall()
        total = sum(r["c"] for r in rows)
        counts = {r["status"]: r["c"] for r in rows}
        return {"total": total, **counts}

    # --------------------------------------------------------- collections
    def create_collection(self, key: str, path: str, name: str, members: list[tuple[str, int]]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO collection(key,path,name,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET path=excluded.path, name=excluded.name",
                (key, path, name, _now()),
            )
            self._conn.execute("DELETE FROM collection_item WHERE collection_key=?", (key,))
            for i, (dkey, ep) in enumerate(members):
                self._conn.execute(
                    "INSERT INTO collection_item(collection_key,ord,dataset_key,episode_index) VALUES(?,?,?,?)",
                    (key, i, dkey, ep),
                )
            self._conn.commit()

    def list_collections(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM collection_item i WHERE i.collection_key=c.key) AS n_items "
                "FROM collection c ORDER BY c.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_collection(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM collection WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def collection_items(self, key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM collection_item WHERE collection_key=? ORDER BY ord", (key,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ template
    def template(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT text FROM template ORDER BY ord").fetchall()
        return [r["text"] for r in rows]

    def set_template(self, texts: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM template")
            for i, t in enumerate(texts):
                self._conn.execute("INSERT INTO template(ord,text) VALUES(?,?)", (i, t))
            self._conn.commit()

    # -------------------------------------------------------------- export
    def create_export(self, name: str, key: str, output: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO export(name,dataset_key,output,status,progress,created_at,updated_at) "
                "VALUES(?,?,?,'running','',?,'')",
                (name, key, output, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_export(self, eid: int, status: str | None = None, progress: str | None = None,
                      report: dict | None = None) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM export WHERE id=?", (eid,)).fetchone()
            if not cur:
                return
            self._conn.execute(
                "UPDATE export SET status=?, progress=?, report=?, updated_at=? WHERE id=?",
                (
                    status if status is not None else cur["status"],
                    progress if progress is not None else cur["progress"],
                    json.dumps(report) if report is not None else cur["report"],
                    _now(),
                    eid,
                ),
            )
            self._conn.commit()

    def get_export(self, eid: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM export WHERE id=?", (eid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["report"] = json.loads(d["report"]) if d.get("report") else None
        return d

    def list_exports(self, key: str | None = None) -> list[dict]:
        with self._lock:
            if key:
                rows = self._conn.execute(
                    "SELECT * FROM export WHERE dataset_key=? ORDER BY id DESC", (key,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM export ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["report"] = json.loads(d["report"]) if d.get("report") else None
            out.append(d)
        return out
