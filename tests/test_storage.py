import sqlite3
import subprocess
import sys

import pytest

from network_history.models import HistoryRow
from network_history.storage.sqlite import SQLiteRepository


def test_import_transaction_rolls_back_history_devices_and_checkpoint(repository):
    with sqlite3.connect(repository.path) as db:
        db.execute(
            """CREATE TRIGGER fail_state BEFORE UPDATE ON collector_state
               BEGIN SELECT RAISE(ABORT, 'simulated disk/commit failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        repository.commit_import([HistoryRow("aa", 100, "x")], 0, 200)
    assert repository.recent_activity() == []
    assert repository.list_devices() == []
    assert repository.get_state().last_imported_timestamp == 0
    assert repository.get_state().last_successful_scrape_at is None


def test_names_survive_discovery_and_reopening(repository):
    repository.commit_import([HistoryRow("aa", 1, "x")], 0, 2)
    assert repository.update_device("aa", "Office", "Main laptop")
    repository.commit_import([HistoryRow("aa", 2, "y"), HistoryRow("bb", 2, "z")], 1, 3)
    reopened = SQLiteRepository(str(repository.path))
    reopened.initialize()
    devices = {d.mac: d for d in reopened.list_devices()}
    assert devices["aa"].friendly_name == "Office"
    assert devices["aa"].notes == "Main laptop"
    assert devices["bb"].friendly_name == ""
    assert reopened.get_state().last_imported_timestamp == 2


def test_recent_order_limit_and_parameterized_filter(repository):
    rows = [HistoryRow("aa", i, "x") for i in range(1, 105)]
    rows += [HistoryRow("bb' OR 1=1 --", 105, "special")]
    repository.commit_import(rows, 0, 200)
    activity = repository.recent_activity()
    assert len(activity) == 100
    assert activity[0].timestamp == 105
    assert activity[-1].timestamp == 6
    filtered = repository.recent_activity("bb' OR 1=1 --")
    assert len(filtered) == 1
    assert filtered[0].url == "special"


def test_stale_checkpoint_refuses_second_import(repository):
    repository.commit_import([HistoryRow("aa", 1, "x")], 0, 2)
    with pytest.raises(RuntimeError, match="Checkpoint changed"):
        repository.commit_import([HistoryRow("aa", 2, "y")], 0, 3)
    assert len(repository.recent_activity()) == 1


def test_null_text_preserved(repository):
    repository.commit_import([HistoryRow(None, 1, None)], 0, 2)
    assert repository.recent_activity()[0].mac is None
    assert repository.recent_activity()[0].url is None
    assert repository.list_devices() == []


def test_os_lock_blocks_another_process_and_releases_after_exit(repository):
    program = (
        "import sys; from pathlib import Path; "
        "from network_history.storage.locking import file_lock; "
        "\nwith file_lock(Path(sys.argv[1])) as acquired: "
        "print('yes' if acquired else 'no')"
    )
    with repository.collection_lock() as acquired:
        assert acquired
        output = subprocess.check_output(
            [sys.executable, "-c", program, str(repository.lock_path)], text=True
        )
        assert output.strip() == "no"
    output = subprocess.check_output(
        [sys.executable, "-c", program, str(repository.lock_path)], text=True
    )
    assert output.strip() == "yes"
