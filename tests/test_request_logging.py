import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from network_history.config import Settings
from network_history.models import HistoryRow
from network_history.queries import QueryError, query_activity
from network_history.request_logging import RequestLogger
from network_history.storage.request_log import SQLiteRequestLog
from network_history.web import create_app


@pytest.fixture
def logged(repository, tmp_path):
    now = int(datetime.now(UTC).timestamp())
    repository.commit_import(
        [
            HistoryRow("aa", now - 120, "twitch.example"),
            HistoryRow("aa", now - 120, "twitch.example"),
            HistoryRow("bb", now - 60, "other.example"),
        ],
        0,
        now,
    )
    settings = Settings(
        database_path=str(repository.path),
        request_log_path=str(tmp_path / "operations.db"),
        api_default_page_size=2,
    )
    app = create_app(settings, repository, Mock())
    app.config["TESTING"] = True
    return app.test_client(), app.extensions["request_logger"], settings


def rows(logger):
    return logger.store.recent(1000)


@pytest.mark.parametrize(
    "path,operation,count",
    [
        ("/api/devices?days=all", "list_devices", 2),
        ("/api/activity", "get_activity", 2),
        ("/api/devices/aa/activity", "get_device_activity", 2),
        ("/api/devices/aa", "get_device_summary", 1),
        ("/api/domains/twitch.example", "get_domain_summary", 1),
        ("/api/activity?domain=absent", "get_activity", 0),
    ],
)
def test_api_operation_is_logged_once(logged, path, operation, count):
    client, logger, _ = logged
    response = client.get(path)
    assert response.status_code == 200
    records = rows(logger)
    assert len(records) == 1
    record = records[0]
    assert record["source"] == "http"
    assert record["operation"] == operation
    assert record["result_count"] == count
    assert record["http_status"] == 200 and record["success"] == 1
    assert record["http_method"] == "GET"
    assert record["http_path"] == path.split("?")[0]
    assert record["duration_ms"] >= 0
    assert abs(record["started_at_ms"] - datetime.now(UTC).timestamp() * 1000) < 10000
    assert record["error_message"] is None
    assert "activity" not in json.loads(record["parameters_json"])


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/api/activity?limit=0", 400, "invalid_parameter"),
        ("/api/activity?limit=99999", 400, "invalid_limit"),
        ("/api/activity?limit=1&limit=2", 400, "invalid_parameter"),
        ("/api/activity?token=TOPSECRET", 400, "invalid_parameter"),
        ("/api/activity?from=invalid", 400, "invalid_datetime"),
        ("/api/devices/missing", 404, "device_not_found"),
        ("/api/domains/missing", 404, "domain_not_found"),
    ],
)
def test_validation_and_not_found_logged_once(logged, path, status, code):
    client, logger, _ = logged
    assert client.get(path).status_code == status
    (record,) = rows(logger)
    assert record["http_status"] == status and record["success"] == 0
    assert record["error_code"] == code and record["error_message"]
    assert record["result_count"] is None
    if "token=TOPSECRET" in path:
        assert json.loads(record["parameters_json"])["token"] == "TOPSECRET"


def test_cursor_records_original_cursor_and_effective_filters(logged):
    client, logger, _ = logged
    first = client.get("/api/activity?limit=1&device=aa").json
    cursor = first["pagination"]["next_cursor"]
    assert client.get("/api/activity", query_string={"cursor": cursor}).status_code == 200
    second, initial = rows(logger)
    assert json.loads(second["parameters_json"]) == {"cursor": cursor}
    effective = json.loads(second["effective_parameters_json"])
    assert effective["device"] == "aa" and effective["limit"] == 1
    assert effective["from"] == first["range"]["from"]
    assert initial["result_count"] == second["result_count"] == 1


def test_unexpected_exception_does_not_leak_message(logged, repository, monkeypatch):
    client, logger, _ = logged

    def fail(*args, **kwargs):
        raise RuntimeError("password=TOPSECRET")

    monkeypatch.setattr(repository, "activity_page", fail)
    with pytest.raises(RuntimeError):
        client.get("/api/activity")
    (record,) = rows(logger)
    assert record["http_status"] == 500 and record["success"] == 0
    assert record["error_message"] == "Query failed (RuntimeError)."
    assert "TOPSECRET" not in str(record)


def test_production_500_is_not_double_logged(logged, repository, monkeypatch):
    client, logger, _ = logged
    client.application.config["TESTING"] = False
    monkeypatch.setattr(repository, "activity_page", Mock(side_effect=RuntimeError("failure")))
    assert client.get("/api/activity").status_code == 500
    assert len(rows(logger)) == 1


def test_ui_interactions_do_not_log(logged):
    client, logger, _ = logged
    for path in [
        "/explore?tab=domain",
        "/explore?tab=device",
        "/health",
        "/request-log",
        "/static/style.css",
        "/explore",
        "/explore?tab=domain&domain=twitch.example&search=1",
        "/explore?tab=device&device=mac:aa&search=1",
        "/",
        "/devices",
    ]:
        assert client.get(path).status_code == 200
    assert client.get("/explore?from=bad").status_code == 400
    assert client.get("/explore?tab=domain&domain=missing&search=1").status_code == 404
    with client.session_transaction() as session:
        token = session["csrf_token"]
    assert (
        client.post(
            "/devices",
            data={"csrf_token": token, "mac": "aa", "friendly_name": "TV", "notes": "Room"},
        ).status_code
        == 303
    )
    assert client.post("/collect", data={"csrf_token": token}).status_code == 303
    assert rows(logger) == []
    assert client.get("/api/devices").status_code == 200
    assert len(rows(logger)) == 1


def test_ui_pagination_does_not_log(logged):
    import re
    from html import unescape

    client, logger, _ = logged
    first = client.get("/explore").text
    next_url = unescape(re.search(r'href="([^"]+)">Next</a>', first)[1])
    second = client.get(next_url).text
    previous = unescape(re.search(r'href="([^"]+)">Previous</a>', second)[1])
    assert client.get(previous).status_code == 200
    assert rows(logger) == []


def test_logging_database_errors_are_isolated_and_retry(logged, monkeypatch, caplog):
    client, logger, _ = logged
    append = logger.store.append
    monkeypatch.setattr(
        logger.store, "append", Mock(side_effect=sqlite3.OperationalError("TOPSECRET"))
    )
    assert client.get("/api/activity").status_code == 200
    assert client.get("/api/devices").status_code == 200
    assert rows(logger) == []
    assert logger.warning
    assert "TOPSECRET" not in caplog.text
    assert caplog.text.count("Request log unavailable") == 1
    assert client.get("/request-log").status_code == 503
    monkeypatch.setattr(logger.store, "append", append)
    logger.retry_at = 0
    assert client.get("/api/activity").status_code == 200
    assert len(rows(logger)) == 1 and logger.warning is None


def test_startup_failure_does_not_stop_web(repository, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    app = create_app(Settings(request_log_path=str(blocked / "log.db")), repository, Mock())
    client = app.test_client()
    assert client.get("/api/devices").status_code == 200
    assert client.get("/request-log").status_code == 503


def test_separate_file_guard_including_hardlinks(repository, tmp_path):
    for target in [repository.path, tmp_path / "alias.db"]:
        if target != repository.path:
            target.hardlink_to(repository.path)
        logger = RequestLogger(SQLiteRequestLog(str(target), str(repository.path)), cooldown=0)
        logger.initialize()
        assert logger.warning
    with sqlite3.connect(repository.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='request_log'").fetchall()


def test_full_url_parameters_are_preserved_but_headers_are_excluded(logged):
    client, logger, _ = logged
    domain = "https://user:TOPSECRET@example.com/path?api_key=HIDDEN#PRIVATE"
    response = client.get(
        "/api/domains/" + quote(domain, safe=""), headers={"Authorization": "Bearer HEADERTOKEN"}
    )
    assert response.status_code == 404
    (record,) = rows(logger)
    assert json.loads(record["parameters_json"]) == {"domain": domain}
    assert record["http_path"] == "/api/domains/" + domain
    assert "HEADERTOKEN" not in str(record)


def test_parameters_are_not_redacted_or_truncated(logged):
    client, logger, _ = logged
    params = {
        "limit": [str(i) for i in range(8)],
        "api_key": "submitted-key",
        "domain": "x" * 3000 + "?token=value#fragment",
        "cursor": "raw-cursor",
    }
    response = client.get("/api/activity", query_string=params)
    assert response.status_code == 400
    assert json.loads(rows(logger)[0]["parameters_json"]) == params


def test_conflicting_path_and_query_values_are_both_preserved(logged):
    client, logger, _ = logged
    assert client.get("/api/devices/aa?mac=bb").status_code == 400
    assert json.loads(rows(logger)[0]["parameters_json"]) == {
        "mac": "aa",
        "query_parameters": {"mac": "bb"},
    }


@pytest.mark.parametrize(
    "value",
    [
        "api_key=do-not-store",
        "Bearer do-not-store",
        "-----BEGIN OPENSSH PRIVATE KEY-----\\nprivate-data",
    ],
)
def test_query_values_are_logged_verbatim(logged, value):
    client, logger, _ = logged
    assert client.get("/api/activity", query_string={"domain": value}).status_code == 200
    record = rows(logger)[0]
    assert json.loads(record["parameters_json"])["domain"] == value
    assert json.loads(record["effective_parameters_json"])["domain"] == value


def test_non_http_callers_reuse_logger(logged, repository):
    _, logger, settings = logged
    with logger.operation(
        source="mcp", operation="get_activity", parameters={"limit": "1"}
    ) as call:
        result = call.result(query_activity(repository, settings, limit="1"))
    (record,) = rows(logger)
    assert len(result["activity"]) == 1
    assert record["source"] == "mcp"
    assert record["http_method"] is None and record["http_status"] is None
    assert record["success"] == 1 and record["result_count"] == 1
    with pytest.raises(QueryError):
        with logger.operation(source="internal", operation="get_activity"):
            raise QueryError("invalid_range", "from must be earlier than to.")
    assert rows(logger)[0]["success"] == 0


def test_parallel_writes_and_persistence(logged):
    _, logger, settings = logged

    def record(index):
        with logger.operation(
            source="internal", operation="list_devices", parameters={"limit": str(index)}
        ) as call:
            call.result({"devices": []})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(20)))
    records = rows(logger)
    assert len(records) == 20
    assert len({r["id"] for r in records}) == 20
    reopened = SQLiteRequestLog(settings.request_log_path, settings.database_path)
    reopened.initialize()
    assert len(reopened.recent(100)) == 20


def test_locked_log_database_does_not_fail_query(logged):
    client, logger, settings = logged
    with sqlite3.connect(settings.request_log_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        assert client.get("/api/activity").status_code == 200
        assert logger.warning
    assert rows(logger) == []


def test_response_rows_and_notes_are_not_persisted(logged, repository):
    client, logger, _ = logged
    repository.update_device("aa", "PRIVATE_FRIENDLY_NAME", "PRIVATE_NOTES")
    response = client.get("/api/devices/aa")
    assert response.json["notes"] == "PRIVATE_NOTES"
    client.get("/api/activity")
    stored = str(rows(logger))
    assert "PRIVATE_NOTES" not in stored and "PRIVATE_FRIENDLY_NAME" not in stored
    assert "twitch.example" not in stored and "other.example" not in stored


@pytest.mark.parametrize("maximum", ["0", "99", "10001", "abc"])
def test_invalid_log_display_settings(monkeypatch, maximum):
    from network_history.config import ConfigurationError

    monkeypatch.setenv("REQUEST_LOG_MAX_DISPLAY", maximum)
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_request_log_environment(monkeypatch, tmp_path):
    path = str(tmp_path / "custom.db")
    monkeypatch.setenv("REQUEST_LOG_PATH", path)
    monkeypatch.setenv("REQUEST_LOG_MAX_DISPLAY", "2000")
    settings = Settings.from_env()
    assert settings.request_log_path == path and settings.request_log_max_display == 2000


def test_bad_log_database_cannot_be_mistaken_for_empty_history(logged, tmp_path):
    _, _, settings = logged
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE important (value TEXT)")
    logger = RequestLogger(SQLiteRequestLog(str(path), settings.database_path))
    logger.initialize()
    assert logger.warning
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='request_log'").fetchall()


def test_deleting_disposable_log_does_not_affect_history(logged, repository):
    client, _, settings = logged
    client.get("/api/activity")
    # All log connections are closed, so SQLite has checkpointed and closed its WAL.
    from pathlib import Path

    Path(settings.request_log_path).unlink()
    assert len(repository.recent_activity()) == 3
    assert repository.get_state().last_scrape_row_count == 3
