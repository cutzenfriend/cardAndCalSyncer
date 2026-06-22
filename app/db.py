"""SQLite store for sync runs and activities."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,            -- sync | discover | clear
    pair         TEXT,                     -- pair name/id or NULL (=all)
    collection   TEXT,                     -- mapping short name or NULL (=whole pair)
    trigger      TEXT NOT NULL,            -- scheduled | manual
    status       TEXT NOT NULL,            -- running | success | failed
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    rc           INTEGER,
    n_create     INTEGER DEFAULT 0,
    n_update     INTEGER DEFAULT 0,
    n_delete     INTEGER DEFAULT 0,
    n_errors     INTEGER DEFAULT 0,
    log          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,             -- create | update | delete
    ident       TEXT NOT NULL,
    pair        TEXT,
    collection  TEXT,                       -- mapping short name
    collection_label TEXT,                  -- real calendar/address-book display name
    title       TEXT,                       -- event/contact title (best-effort)
    subtitle    TEXT,                       -- date / extra detail (best-effort)
    src_name    TEXT,                       -- source account (name)
    src_kind    TEXT,                       -- icloud | google | caldav
    dst_name    TEXT,                       -- target account (name)
    dst_kind    TEXT
);

CREATE INDEX IF NOT EXISTS idx_act_run ON activities(run_id);
CREATE INDEX IF NOT EXISTS idx_act_ts  ON activities(ts);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing DBs."""
        acols = {r["name"] for r in self._conn.execute("PRAGMA table_info(activities)")}
        for col in ("collection_label", "title", "subtitle"):
            if col not in acols:
                self._conn.execute(f"ALTER TABLE activities ADD COLUMN {col} TEXT")
        rcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "collection" not in rcols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN collection TEXT")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # --- Runs --------------------------------------------------------------
    def start_run(self, kind: str, pair: str | None, trigger: str, started_at: str,
                  collection: str | None = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (kind, pair, collection, trigger, status, started_at) "
                "VALUES (?,?,?,?,?,?)",
                (kind, pair, collection, trigger, "running", started_at),
            )
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, *, status: str, finished_at: str, rc: int,
        n_create: int, n_update: int, n_delete: int, n_errors: int, log: str,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE runs SET status=?, finished_at=?, rc=?, n_create=?, "
                "n_update=?, n_delete=?, n_errors=?, log=? WHERE id=?",
                (status, finished_at, rc, n_create, n_update, n_delete, n_errors, log, run_id),
            )

    def add_activity(self, run_id: int, ts: str, act: dict[str, Any]) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO activities (run_id, ts, action, ident, pair, collection, "
                "collection_label, title, subtitle, src_name, src_kind, dst_name, dst_kind) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, ts, act["action"], act["ident"], act.get("pair"),
                    act.get("collection"), act.get("collection_label"),
                    act.get("title"), act.get("subtitle"),
                    act.get("src_name"), act.get("src_kind"),
                    act.get("dst_name"), act.get("dst_kind"),
                ),
            )
            return int(cur.lastrowid)

    def set_activity_detail(self, activity_id: int, title: str | None,
                            subtitle: str | None) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE activities SET title=?, subtitle=? WHERE id=?",
                        (title, subtitle, activity_id))

    def list_runs(self, limit: int = 50, offset: int = 0, kind: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM runs"
        params: list[Any] = []
        if kind:
            q += " WHERE kind=?"
            params.append(kind)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id=?", (run_id,))
            return cur.fetchone()

    def run_activities(self, run_id: int) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM activities WHERE run_id=? ORDER BY id", (run_id,))
            return cur.fetchall()

    def recent_activities(self, limit: int = 100, action: str | None = None,
                          pair: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM activities"
        where, params = [], []
        if action:
            where.append("action=?"); params.append(action)
        if pair:
            where.append("pair=?"); params.append(pair)
        if since:
            where.append("ts>=?"); params.append(since)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def clear_activities(self, since: str | None = None) -> int:
        """Delete activity rows (all, or only those older than `since`)."""
        with self._cursor() as cur:
            if since:
                cur.execute("DELETE FROM activities WHERE ts < ?", (since,))
            else:
                cur.execute("DELETE FROM activities")
            return cur.rowcount

    def last_run(self, kind: str = "sync") -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM runs WHERE kind=? AND status!='running' "
                "ORDER BY id DESC LIMIT 1", (kind,))
            return cur.fetchone()

    def stats(self) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(n_create),0) c, COALESCE(SUM(n_update),0) u, "
                        "COALESCE(SUM(n_delete),0) d FROM runs WHERE kind='sync'")
            r = cur.fetchone()
            return {"create": r["c"], "update": r["u"], "delete": r["d"]}

    def prune_runs(self, keep: int = 500) -> None:
        """Delete runs older than the newest `keep` (activities via cascade)."""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM runs WHERE id NOT IN "
                "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (keep,))
