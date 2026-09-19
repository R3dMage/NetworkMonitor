import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from network_history.models import HistoryRow
from network_history.storage.sqlite import SQLiteRepository


def make_version_one(repository):
    # v2 changes only this index and user_version. Remove them to recreate v1.
    with sqlite3.connect(repository.path) as db:
        db.execute("DROP INDEX history_domain")
        db.execute("PRAGMA user_version=1")


def version(repository):
    with sqlite3.connect(repository.path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_index_migration_preserves_rows_ids_devices_and_collector_state(repository):
    repository.commit_import([HistoryRow("aa", 10, "example")] * 2, 0, 20)
    repository.update_device("aa", "Laptop", "Notes")
    repository.record_error("old error")
    rows, devices, state = (
        repository.recent_activity(),
        repository.list_devices(),
        repository.get_state(),
    )
    make_version_one(repository)
    upgraded = SQLiteRepository(str(repository.path))
    upgraded.initialize()
    assert version(repository) == 2
    assert upgraded.recent_activity() == rows
    assert upgraded.list_devices() == devices
    assert upgraded.get_state() == state
    with sqlite3.connect(repository.path) as db:
        columns = [row[2] for row in db.execute("PRAGMA index_info(history_domain)")]
    assert columns == ["url", "timestamp", "id", "mac"]
    upgraded.initialize()
    assert version(repository) == 2


def test_failed_migration_rolls_back_version_and_keeps_data(repository):
    repository.commit_import([HistoryRow("aa", 10, "example")], 0, 20)
    make_version_one(repository)
    with sqlite3.connect(repository.path) as db:
        db.execute("CREATE TABLE history_domain(blocks_index_name TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        repository.initialize()
    assert version(repository) == 1
    assert repository.get_state().last_imported_timestamp == 10
    assert len(repository.recent_activity()) == 1
    with sqlite3.connect(repository.path) as db:
        db.execute("DROP TABLE history_domain")
    repository.initialize()
    assert version(repository) == 2


def test_concurrent_initialization_rechecks_version_after_lock(repository):
    make_version_one(repository)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(SQLiteRepository(str(repository.path)).initialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert version(repository) == 2


def test_unknown_future_schema_still_fails_explicitly(repository):
    with sqlite3.connect(repository.path) as db:
        db.execute("PRAGMA user_version=3")
    with pytest.raises(RuntimeError, match="Unsupported database schema version: 3"):
        repository.initialize()
