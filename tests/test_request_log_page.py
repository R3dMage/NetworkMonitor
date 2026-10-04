from unittest.mock import Mock

import pytest

from network_history.config import Settings
from network_history.web import create_app


@pytest.fixture
def log_page(repository, tmp_path):
    app = create_app(
        Settings(
            request_log_path=str(tmp_path / "request_log.db"),
            database_path=str(repository.path),
        ),
        repository,
        Mock(),
    )
    app.config["TESTING"] = True
    logger = app.extensions["request_logger"]
    for index in range(105):
        scope = logger.begin(
            source="http",
            operation="list_devices",
            parameters={"days": "all"},
            http_method="GET",
            http_path=f"/api/devices/{index + 1}",
        )
        scope.result({"devices": []})
        scope.record["started_at_ms"] = 1704067200000 + index
        scope.finish(http_status=200)
    return app.test_client(), logger


def test_default_larger_limit_and_order(log_page):
    client, logger = log_page
    response = client.get("/request-log")
    assert response.status_code == 200
    assert "Showing 100 calls." in response.text
    assert response.text.index("/api/devices/105<") < response.text.index("/api/devices/104<")
    assert "/api/devices/5<" not in response.text
    assert '<td class="log-time">2023-12-31 19:00:00</td>' in response.text
    assert "Times shown in America/New_York" in response.text
    assert "<details" not in response.text
    assert "Effective filters" not in response.text
    assert "Source / operation" not in response.text
    assert "#105" not in response.text
    assert "list_devices" not in response.text
    assert "GET" not in response.text
    assert '<span class="muted">days:</span> all' in response.text
    assert "Showing 105 calls." in client.get("/request-log?limit=200").text
    assert len(logger.store.recent(1000)) == 105
    assert 'href="/request-log"' in client.get("/devices").text


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", "", "1001", "99999999999999999"])
def test_invalid_limits_are_not_clamped(log_page, value):
    client, _ = log_page
    response = client.get("/request-log", query_string={"limit": value})
    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert f'value="{value}"' in response.text
    assert "Showing 100 calls." not in response.text
    if value == "1001":
        assert "at most 1000" in response.text


def test_repeated_limit_is_rejected(log_page):
    client, _ = log_page
    assert client.get("/request-log?limit=1&limit=2").status_code == 400


def test_inline_parameters_and_errors_escape_untrusted_text(log_page):
    client, logger = log_page
    scope = logger.begin(
        source="http",
        operation="get_domain_summary",
        parameters={"domain": "<script>alert(1)</script>"},
    )
    scope.finish(http_status=404)
    response = client.get("/request-log")
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text
    assert "<details" not in response.text
    assert "The request could not be completed." in response.text
    assert 'class="log-error-row"' in response.text
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


def test_log_read_failure_is_visible(log_page, monkeypatch):
    client, logger = log_page
    monkeypatch.setattr(logger.store, "recent", Mock(side_effect=OSError("private detail")))
    response = client.get("/request-log")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    assert "private detail" not in response.text
    assert "No calls recorded yet" not in response.text
