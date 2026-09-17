import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from network_history.models import (
    Activity,
    CollectorState,
    Device,
    HistoryPage,
    HistoryRow,
    StoredActivity,
)
from network_history.storage.locking import file_lock


class SQLiteRepository:
    def __init__(self, path: str):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_name(self.path.name + ".collect.lock")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            # Existing databases need no writer lock just to start a collection process.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if version != 0:
                raise RuntimeError(f"Unsupported database schema version: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            if version == 0:
                statements = (
                    """CREATE TABLE history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mac TEXT, timestamp INTEGER NOT NULL, url TEXT
                    )""",
                    """CREATE TABLE devices (
                        mac TEXT PRIMARY KEY NOT NULL,
                        friendly_name TEXT NOT NULL DEFAULT '', notes TEXT
                    )""",
                    """CREATE TABLE collector_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        last_imported_timestamp INTEGER NOT NULL DEFAULT 0,
                        last_successful_scrape_at INTEGER,
                        last_scrape_row_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT
                    )""",
                    "INSERT INTO collector_state (id) VALUES (1)",
                    "CREATE INDEX history_recent ON history (timestamp DESC, id DESC)",
                    "CREATE INDEX history_device ON history (mac, timestamp DESC, id DESC)",
                    "PRAGMA user_version=1",
                )
                for statement in statements:
                    db.execute(statement)

    def collection_lock(self):
        return file_lock(self.lock_path)

    def collection_running(self) -> bool:
        with self.collection_lock() as acquired:
            return not acquired

    def get_state(self) -> CollectorState:
        with self._connection() as db:
            row = db.execute(
                """SELECT last_imported_timestamp, last_successful_scrape_at,
                          last_scrape_row_count, last_error
                   FROM collector_state WHERE id=1"""
            ).fetchone()
            return CollectorState(**dict(row))

    def commit_import(
        self, rows: Sequence[HistoryRow], expected_checkpoint: int, successful_at: int
    ) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            checkpoint = db.execute(
                "SELECT last_imported_timestamp FROM collector_state WHERE id=1"
            ).fetchone()[0]
            if checkpoint != expected_checkpoint:
                raise RuntimeError("Checkpoint changed during collection; import rolled back")
            if any(row.timestamp <= checkpoint for row in rows):
                raise ValueError("Import contains a row at or before the checkpoint")
            db.executemany(
                "INSERT INTO history (mac, timestamp, url) VALUES (?, ?, ?)",
                ((row.mac, row.timestamp, row.url) for row in rows),
            )
            db.executemany(
                "INSERT INTO devices (mac) VALUES (?) ON CONFLICT(mac) DO NOTHING",
                ((row.mac,) for row in rows if row.mac is not None),
            )
            new_checkpoint = max((row.timestamp for row in rows), default=checkpoint)
            db.execute(
                """UPDATE collector_state SET last_imported_timestamp=?,
                   last_successful_scrape_at=?, last_scrape_row_count=?, last_error=NULL
                   WHERE id=1""",
                (new_checkpoint, successful_at, len(rows)),
            )

    def record_error(self, error: str) -> None:
        with self._connection() as db:
            db.execute("UPDATE collector_state SET last_error=? WHERE id=1", (error[:4000],))

    def recent_activity(self, mac: str | None = None, limit: int = 100) -> list[Activity]:
        query = """SELECT h.id, h.mac, h.timestamp, h.url, d.friendly_name
                   FROM history h LEFT JOIN devices d ON d.mac=h.mac"""
        parameters: list = []
        if mac is not None:
            query += " WHERE h.mac=?"
            parameters.append(mac)
        query += " ORDER BY h.timestamp DESC, h.id DESC LIMIT ?"
        parameters.append(limit)
        with self._connection() as db:
            return [Activity(**dict(row)) for row in db.execute(query, parameters)]

    def list_devices(self) -> list[Device]:
        with self._connection() as db:
            return [
                Device(**dict(row))
                for row in db.execute(
                    """SELECT mac, friendly_name, notes FROM devices
                       ORDER BY CASE WHEN friendly_name='' THEN 1 ELSE 0 END,
                       friendly_name COLLATE NOCASE, mac"""
                )
            ]

    def update_device(self, mac: str, friendly_name: str, notes: str | None) -> bool:
        with self._connection() as db:
            result = db.execute(
                "UPDATE devices SET friendly_name=?, notes=? WHERE mac=?",
                (friendly_name, notes, mac),
            )
            return result.rowcount == 1

    def find_device(self, mac: str) -> Device | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT mac, friendly_name, notes FROM devices WHERE mac=?", (mac,)
            ).fetchone()
            return Device(**dict(row)) if row is not None else None

    def devices_seen_between(self, start: int, end: int) -> list[Device]:
        with self._connection() as db:
            return [
                Device(**dict(row))
                for row in db.execute(
                    """SELECT d.mac, d.friendly_name, d.notes FROM devices d
                       WHERE EXISTS (
                           SELECT 1 FROM history h WHERE h.mac=d.mac
                           AND h.timestamp >= ? AND h.timestamp < ?
                       )
                       ORDER BY CASE WHEN d.friendly_name='' THEN 1 ELSE 0 END,
                       d.friendly_name COLLATE NOCASE, d.mac""",
                    (start, end),
                )
            ]

    def activity_page(
        self,
        mac: str,
        start: int,
        end: int,
        limit: int,
        *,
        after: tuple[int, int] | None = None,
        snapshot_id: int | None = None,
    ) -> HistoryPage:
        with self._connection() as db:
            # A read transaction pins the first page and its upper ID to one snapshot.
            db.execute("BEGIN")
            if snapshot_id is None:
                snapshot_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM history").fetchone()[0]
            query = """SELECT id, timestamp, url FROM history
                       WHERE mac=? AND timestamp >= ? AND timestamp < ? AND id <= ?"""
            parameters = [mac, start, end, snapshot_id]
            if after is not None:
                query += " AND (timestamp, id) > (?, ?)"
                parameters.extend(after)
            query += " ORDER BY timestamp ASC, id ASC LIMIT ?"
            parameters.append(limit)
            rows = [StoredActivity(**dict(row)) for row in db.execute(query, parameters)]
            return HistoryPage(rows=rows, snapshot_id=snapshot_id)
