import json
import shlex
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from network_history.config import Settings
from network_history.router_source import (
    RouterError,
    history_query,
    parse_rows,
    read_channel,
    remote_command,
)


def test_shell_arguments_are_quoted_and_sql_is_not_in_command():
    settings = replace(
        Settings(),
        router_db_path="/jffs/a'; touch /tmp/should-not-exist; #.db",
        router_sqlite_bin="/opt/sqlite tools/sqlite3",
    )
    arguments = shlex.split(remote_command(settings))
    assert arguments == [
        settings.router_sqlite_bin,
        "-init",
        "/dev/null",
        "-readonly",
        "-batch",
        "-bail",
        "-json",
        settings.router_db_path,
    ]
    assert "SELECT" not in remote_command(settings)
    assert history_query(5, 20).endswith("timestamp > 5 AND timestamp <= 20 ORDER BY timestamp;\n")
    with pytest.raises(ValueError):
        history_query("0; DROP TABLE history", 10)


def test_json_preserves_duplicate_and_special_text():
    row = {"mac": "aa", "timestamp": 10, "url": 'one\ttwo\n"quotes" \\ unicode: é'}
    rows = parse_rows(json.dumps([row, row]).encode(), 0, 10)
    assert len(rows) == 2 and rows[0] == rows[1]
    assert rows[0].url == row["url"]
    assert parse_rows(b"", 0, 10) == []


@pytest.mark.parametrize(
    "output",
    [
        b"[",
        b"{}",
        b'[{"mac":"aa","timestamp":0,"url":"x"}]',
        b'[{"mac":"aa","timestamp":11,"url":"x"}]',
        b'[{"mac":"aa","timestamp":true,"url":"x"}]',
        b'[{"mac":"aa","timestamp":"5","url":"x"}]',
        b'[{"mac":1,"timestamp":5,"url":"x"}]',
    ],
)
def test_bad_or_partial_output_never_returns_partial_rows(output):
    with pytest.raises(RouterError):
        parse_rows(output, 0, 10)


def test_read_channel_drains_both_streams_before_exit():
    channel = Mock()
    channel.eof_received = True
    channel.closed = False
    stdout = [b"[]"]
    stderr = [b"failure"]
    channel.recv_ready.side_effect = lambda: bool(stdout)
    channel.recv_stderr_ready.side_effect = lambda: bool(stderr)
    channel.recv.side_effect = lambda _: stdout.pop()
    channel.recv_stderr.side_effect = lambda _: stderr.pop()
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 1
    assert read_channel(channel, 10) == (b"[]", b"failure", 1)


def test_remote_command_deadline_is_enforced():
    with patch("network_history.router_source.time.monotonic", side_effect=[0, 2]):
        with pytest.raises(RouterError, match="timed out"):
            read_channel(Mock(), timeout=1)


def test_eof_before_exit_status_is_not_treated_as_a_failed_scrape():
    channel = Mock()
    channel.eof_received = True
    channel.closed = False
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = False
    channel.recv_exit_status.return_value = 0
    assert read_channel(channel, timeout=1) == (b"", b"", 0)
