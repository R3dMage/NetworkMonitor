from unittest.mock import Mock

import pytest

from network_history.config import Settings
from network_history.models import HistoryRow
from network_history.web import create_app


@pytest.fixture
def web(repository):
    manual = Mock()
    manual.start.return_value = True
    app = create_app(Settings(timezone="America/New_York"), repository, manual)
    app.config["TESTING"] = True
    return app.test_client(), manual


def form_token(client):
    client.get("/")
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_activity_has_names_raw_fields_escaping_and_local_timezone(repository, web):
    client, _ = web
    repository.commit_import(
        [HistoryRow("aa", 1704067200, '<script>alert("x")</script>')], 0, 1704067201
    )
    repository.update_device("aa", "Living room", None)
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Living room" in html and "<code>aa</code>" in html
    assert "2023-12-31 19:00:00 EST (-0500)" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/health").json == {"status": "ok"}


def test_filter_and_edit_devices(repository, web):
    client, _ = web
    repository.commit_import(
        [HistoryRow("aa", 1, "one.example"), HistoryRow("bb", 2, "two.example")], 0, 3
    )
    html = client.get("/?mac=aa").get_data(as_text=True)
    assert "one.example" in html and "two.example" not in html
    token = form_token(client)
    response = client.post(
        "/devices",
        data={"csrf_token": token, "mac": "aa", "friendly_name": "Laptop", "notes": "Work"},
    )
    assert response.status_code == 303
    assert "Laptop" in client.get("/devices").get_data(as_text=True)
    assert next(d for d in repository.list_devices() if d.mac == "aa").notes == "Work"


def test_mutations_require_csrf(web):
    client, manual = web
    assert client.post("/collect").status_code == 400
    assert client.post("/devices", data={"mac": "aa"}).status_code == 400
    manual.start.assert_not_called()


def test_manual_action_returns_without_running_collection_in_request(web):
    client, manual = web
    token = form_token(client)
    response = client.post("/collect", data={"csrf_token": token})
    assert response.status_code == 303
    manual.start.assert_called_once_with()


def test_manual_action_does_not_spawn_when_scheduled_collection_holds_lock(repository, web):
    client, manual = web
    token = form_token(client)
    with repository.collection_lock() as acquired:
        assert acquired
        assert client.post("/collect", data={"csrf_token": token}).status_code == 303
    manual.start.assert_not_called()


def test_non_ascii_form_token_is_rejected_without_server_error(web):
    client, manual = web
    form_token(client)
    assert client.post("/collect", data={"csrf_token": "\u00e9"}).status_code == 400
    manual.start.assert_not_called()
