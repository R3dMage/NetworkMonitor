from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from network_history.config import Settings
from network_history.models import HistoryRow
from network_history.web import create_app

MAC = "AA:BB:CC:DD:EE:01"


@pytest.fixture
def client(repository):
    now = int(datetime.now(UTC).timestamp())
    repository.commit_import(
        [
            HistoryRow(MAC, now - 60, "one.example"),
            HistoryRow(MAC, now - 60, "one.example"),
            HistoryRow(MAC, now - 30, "two.example"),
            HistoryRow("old-device", now - 86400 * 40, "old.example"),
        ],
        0,
        now,
    )
    app = create_app(Settings(), repository, Mock())
    app.config["TESTING"] = True
    return app.test_client()


def test_json_listing_defaults_and_all(client):
    response = client.get("/api/devices")
    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.json["devices"] == [{"mac": MAC, "friendly_name": None, "notes": None}]
    assert response.json["range"]["to"].endswith("Z")
    assert "Set-Cookie" not in response.headers
    assert len(client.get("/api/devices?days=all").json["devices"]) == 2


def test_activity_pages_through_http(client):
    response = client.get(f"/api/devices/{MAC}/activity?limit=2")
    assert response.status_code == 200
    assert len(response.json["activity"]) == 2
    cursor = response.json["pagination"]["next_cursor"]
    second = client.get(f"/api/devices/{MAC}/activity", query_string={"cursor": cursor})
    assert second.status_code == 200
    assert second.json["range"] == response.json["range"]
    assert second.json["activity"][0]["domain"] == "two.example"
    assert second.json["pagination"]["next_cursor"] is None
    assert second.json["pagination"]["has_more"] is False


@pytest.mark.parametrize(
    "suffix",
    [
        "?limit=0",
        "?limit=2001",
        "?limit=1&limit=2",
        "?unknown=1",
        "?from=",
        "?from=2026-09-01T00:00:00",
        "?cursor=bad",
        "?from=2026-01-01T00:00:00Z&to=2026-09-01T00:00:00Z",
    ],
)
def test_api_errors_are_json(client, suffix):
    response = client.get(f"/api/devices/{MAC}/activity{suffix}")
    assert response.status_code == 400
    assert set(response.json["error"]) == {"code", "message"}


@pytest.mark.parametrize("suffix", ["?days=-1", "?days=1&days=2", "?from=2026-01-01"])
def test_device_filter_errors_are_json(client, suffix):
    response = client.get("/api/devices" + suffix)
    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_parameter"


def test_missing_device_404_but_inactive_known_device_200(client):
    response = client.get("/api/devices/missing/activity")
    assert response.status_code == 404
    assert response.json["error"]["code"] == "device_not_found"
    response = client.get("/api/devices/old-device/activity")
    assert response.status_code == 200
    assert response.json["activity"] == []


def test_url_encoded_positive_offset(client):
    now = datetime.now(UTC).replace(microsecond=0)
    offset = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+02:00")
    response = client.get(f"/api/devices/{MAC}/activity", query_string={"to": offset})
    assert response.status_code == 200
    assert response.json["range"]["to"] == now.isoformat().replace("+00:00", "Z")


def test_html_edits_are_immediately_visible_through_api(client):
    client.get("/devices")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/devices",
        data={"csrf_token": token, "mac": MAC, "friendly_name": "Office laptop", "notes": "Work"},
    )
    assert response.status_code == 303
    assert client.get("/api/devices").json["devices"][0] == {
        "mac": MAC,
        "friendly_name": "Office laptop",
        "notes": "Work",
    }
    assert (
        client.get(f"/api/devices/{MAC}/activity").json["device"]["friendly_name"]
        == "Office laptop"
    )
    assert "Office laptop" in client.get("/").get_data(as_text=True)
    assert "old-device" in client.get("/devices").get_data(as_text=True)


def test_api_reads_do_not_change_collector_state(client, repository):
    before = repository.get_state()
    client.get("/api/devices")
    client.get(f"/api/devices/{MAC}/activity")
    assert repository.get_state() == before
