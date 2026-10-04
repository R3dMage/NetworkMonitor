"""Disposable operational storage, independent of the network-history repository."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class RequestLogStore(Protocol):
    def initialize(self) -> None: ...
    def append(self, record: dict) -> None: ...
    def recent(self, limit: int) -> list[dict]: ...


class SQLiteRequestLog:
    def __init__(self, path: str, history_path: str):
        self.path = Path(path)
        self.history_path = Path(history_path)

    def _check_path(self):
        if str(self.path) in {".", ":memory:"}:
            raise ValueError("Request log must name a persistent database file")
        if self.path.resolve() == self.history_path.resolve() or (
            self.path.exists()
            and self.history_path.exists()
            and self.path.samefile(self.history_path)
        ):
            raise ValueError("Request log must be separate from network history")

    @contextmanager
    def _connection(self, *, readonly=False):
        self._check_path()
        target = self.path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
        db = sqlite3.connect(target, uri=True, timeout=0.025)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables - {"request_log"} or version not in (0, 1):
                raise ValueError("Not a supported request log database")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_ms INTEGER NOT NULL,
                source TEXT NOT NULL,
                operation TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                effective_parameters_json TEXT,
                http_method TEXT,
                http_path TEXT,
                success INTEGER NOT NULL CHECK (success IN (0, 1)),
                http_status INTEGER,
                result_count INTEGER,
                duration_ms REAL NOT NULL,
                error_code TEXT,
                error_message TEXT
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS request_log_recent
                ON request_log (started_at_ms DESC, id DESC)""")
            db.execute("PRAGMA user_version=1")

    def append(self, record):
        with self._connection() as db:
            db.execute(
                """INSERT INTO request_log (
                started_at_ms, source, operation, parameters_json, effective_parameters_json,
                http_method, http_path, success, http_status, result_count, duration_ms,
                error_code, error_message
            ) VALUES (
                :started_at_ms, :source, :operation, :parameters_json, :effective_parameters_json,
                :http_method, :http_path, :success, :http_status, :result_count, :duration_ms,
                :error_code, :error_message
            )""",
                record,
            )

    def recent(self, limit):
        with self._connection(readonly=True) as db:
            rows = db.execute(
                """SELECT * FROM request_log
                ORDER BY started_at_ms DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for field in ("parameters_json", "effective_parameters_json"):
                item[field] = json.dumps(json.loads(item[field]), indent=2) if item[field] else ""
            item["timestamp"] = item["started_at_ms"] / 1000
            results.append(item)
        return results
