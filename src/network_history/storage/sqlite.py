import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from network_history.models import (
    Activity,
    CollectorState,
    Device,
    DeviceSummary,
    DomainDeviceSummary,
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
            if version == 2:
                return
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            if version == 0:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            # Another process may have migrated while we waited for the writer lock.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
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
                version = 1
            if version == 1:
                db.execute("CREATE INDEX history_domain ON history (url, timestamp, id, mac)")
                db.execute("PRAGMA user_version=2")

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
        mac: str | None,
        start: int,
        end: int,
        limit: int,
        *,
        domain: str | None = None,
        after: tuple[int, int] | None = None,
        snapshot_id: int | None = None,
    ) -> HistoryPage:
        with self._connection() as db:
            # A read transaction pins the first page and its upper ID to one snapshot.
            db.execute("BEGIN")
            if snapshot_id is None:
                snapshot_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM history").fetchone()[0]
            query = """SELECT h.id, h.timestamp, h.url, h.mac, d.friendly_name
                       FROM history h LEFT JOIN devices d ON d.mac=h.mac
                       WHERE h.timestamp >= ? AND h.timestamp < ? AND h.id <= ?"""
            parameters = [start, end, snapshot_id]
            if mac is not None:
                query += " AND h.mac=?"
                parameters.append(mac)
            if domain is not None:
                query += " AND h.url=?"
                parameters.append(domain)
            if after is not None:
                query += " AND (h.timestamp, h.id) > (?, ?)"
                parameters.extend(after)
            query += " ORDER BY h.timestamp ASC, h.id ASC LIMIT ?"
            parameters.append(limit)
            rows = [StoredActivity(**dict(row)) for row in db.execute(query, parameters)]
            return HistoryPage(rows=rows, snapshot_id=snapshot_id)

    def device_summary(self, mac: str) -> DeviceSummary | None:
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute(
                """SELECT d.mac, d.friendly_name, d.notes,
                          MIN(h.timestamp) AS first_seen, MAX(h.timestamp) AS last_seen,
                          COUNT(h.id) AS total_observation_count,
                          COUNT(DISTINCT h.url) AS distinct_domain_count
                   FROM devices d LEFT JOIN history h ON h.mac=d.mac
                   WHERE d.mac=?
                   GROUP BY d.mac, d.friendly_name, d.notes""",
                (mac,),
            ).fetchone()
            return DeviceSummary(**dict(row)) if row is not None else None

    def domain_device_summaries(self, domain: str) -> list[DomainDeviceSummary]:
        with self._connection() as db:
            db.execute("BEGIN")
            # One grouped scan supplies both overall and per-device facts. The query
            # layer combines these groups, so totals cannot disagree during imports.
            return [
                DomainDeviceSummary(**dict(row))
                for row in db.execute(
                    """SELECT h.mac, d.friendly_name,
                              MIN(h.timestamp) AS first_seen, MAX(h.timestamp) AS last_seen,
                              COUNT(*) AS observation_count
                       FROM history h LEFT JOIN devices d ON d.mac=h.mac
                       WHERE h.url=?
                       GROUP BY h.mac, d.friendly_name
                       ORDER BY h.mac IS NULL, h.mac""",
                    (domain,),
                )
            ]
