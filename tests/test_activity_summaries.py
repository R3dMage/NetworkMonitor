import base64
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from network_history.config import Settings
from network_history.models import HistoryRow
from network_history.queries import (
    QueryError,
    get_device_summary,
    get_domain_summary,
    query_activity,
    query_device_activity,
)
from network_history.web import create_app

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
START = int((NOW - timedelta(days=7)).timestamp())
END = int(NOW.timestamp())
MAC = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"
DOMAIN = "netflix.com"
FULL_URL = "https://example.com/a/b?x=1&y=2#part"
SETTINGS = Settings()


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def populated(repository):
    repository.commit_import(
        [
            HistoryRow(MAC, START - 86400 * 40, DOMAIN),
            HistoryRow(MAC, START, DOMAIN),
            HistoryRow(MAC, START, DOMAIN),
            HistoryRow(OTHER, START, DOMAIN),
            HistoryRow(MAC, START + 1, "www.netflix.com"),
            HistoryRow(MAC, START + 2, "Netflix.com"),
            HistoryRow(MAC, START + 3, FULL_URL),
            HistoryRow(MAC, START + 4, None),
            HistoryRow(None, START + 5, DOMAIN),
            HistoryRow("", START + 6, ""),
            HistoryRow(OTHER, END, DOMAIN),
        ],
        0,
        END,
    )
    repository.update_device(MAC, "Living room TV", "Main screen")
    with sqlite3.connect(repository.path) as db:
        db.execute("INSERT INTO devices(mac) VALUES ('empty-device')")
    return repository


def test_unfiltered_observations_are_bounded_ordered_and_keep_nulls(populated):
    result = query_activity(populated, SETTINGS, now=NOW)
    assert result["filters"] == {"device": None, "domain": None}
    assert result["range"] == {"from": iso(START), "to": iso(END)}
    assert len(result["activity"]) == 9
    assert result["activity"][0] == result["activity"][1]
    assert result["activity"][0] == {
        "timestamp": iso(START),
        "mac": MAC,
        "friendly_name": "Living room TV",
        "domain": DOMAIN,
    }
    assert result["activity"][2]["mac"] == OTHER
    assert result["activity"][-2]["mac"] is None
    assert any(row["domain"] is None for row in result["activity"])
    assert [row["timestamp"] for row in result["activity"]] == sorted(
        row["timestamp"] for row in result["activity"]
    )


@pytest.mark.parametrize(
    "filters,count",
    [
        ({"device": MAC}, 6),
        ({"domain": DOMAIN}, 4),
        ({"device": MAC, "domain": DOMAIN}, 2),
        ({"device": OTHER, "domain": DOMAIN}, 1),
        ({"domain": "NETFLIX.COM"}, 0),
        ({"domain": "netflix"}, 0),
        ({"device": "missing"}, 0),
        ({"domain": "missing"}, 0),
        ({"device": "' OR 1=1 --", "domain": DOMAIN}, 0),
        ({"domain": "' OR 1=1 --"}, 0),
        ({"domain": FULL_URL}, 1),
        ({"device": "", "domain": ""}, 1),
    ],
)
def test_filter_combinations_are_exact_and_parameterized(populated, filters, count):
    result = query_activity(populated, SETTINGS, now=NOW, **filters)
    assert len(result["activity"]) == count
    assert result["filters"] == {"device": None, "domain": None} | filters
    assert result["pagination"]["has_more"] is False


def test_general_cursor_freezes_filters_window_and_import_snapshot(repository):
    timestamp = START + 10
    repository.commit_import(
        [
            HistoryRow(MAC, timestamp, DOMAIN),
            HistoryRow(MAC, timestamp, DOMAIN),
            HistoryRow(MAC, timestamp, DOMAIN),
            HistoryRow(OTHER, timestamp, DOMAIN),
            HistoryRow(MAC, timestamp, "other.example"),
        ],
        0,
        timestamp,
    )
    result = query_activity(repository, SETTINGS, device=MAC, domain=DOMAIN, limit="1", now=NOW)
    repository.commit_import([HistoryRow(MAC, timestamp + 1, DOMAIN)], timestamp, timestamp + 1)
    pages = [result]
    while pages[-1]["pagination"]["has_more"]:
        pages.append(
            query_activity(
                repository,
                SETTINGS,
                cursor=pages[-1]["pagination"]["next_cursor"],
                now=NOW + timedelta(days=60),
            )
        )
    assert len(pages) == 3
    assert all(p["filters"] == {"device": MAC, "domain": DOMAIN} for p in pages)
    assert all(p["range"] == result["range"] for p in pages)
    assert all(p["activity"] == result["activity"] for p in pages)
    assert pages[-1]["pagination"]["next_cursor"] is None
    assert (
        len(query_activity(repository, SETTINGS, device=MAC, domain=DOMAIN, now=NOW)["activity"])
        == 4
    )


def test_existing_v1_cursor_from_before_upgrade_is_accepted(populated):
    # Construct the old public cursor format independently of the new encoder.
    with sqlite3.connect(populated.path) as db:
        snapshot = db.execute("SELECT MAX(id) FROM history").fetchone()[0]
        first_id = db.execute(
            "SELECT id FROM history WHERE mac=? AND timestamp=? ORDER BY id LIMIT 1", (MAC, START)
        ).fetchone()[0]
    payload = {
        "v": 1,
        "mac": MAC,
        "from": iso(START),
        "to": iso(END),
        "limit": 1,
        "snapshot": snapshot,
        "timestamp": START,
        "id": first_id,
    }
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    result = query_device_activity(populated, SETTINGS, MAC, cursor=token)
    assert set(result) == {"device", "range", "activity", "pagination"}
    assert result["activity"] == [{"timestamp": iso(START), "domain": DOMAIN}]


def test_cursor_formats_cannot_be_cross_used(populated):
    general = query_activity(populated, SETTINGS, limit="1", now=NOW)
    legacy = query_device_activity(populated, SETTINGS, MAC, limit="1", now=NOW)
    with pytest.raises(QueryError, match="Invalid cursor"):
        query_activity(populated, SETTINGS, cursor=legacy["pagination"]["next_cursor"])
    with pytest.raises(QueryError, match="Invalid cursor"):
        query_device_activity(populated, SETTINGS, MAC, cursor=general["pagination"]["next_cursor"])


@pytest.mark.parametrize(
    "extra",
    [
        {"device": MAC},
        {"domain": DOMAIN},
        {"limit": "2"},
        {"from_time": iso(START)},
        {"to_time": iso(END)},
    ],
)
def test_cursor_cannot_be_combined_with_filters(populated, extra):
    page = query_activity(populated, SETTINGS, limit="1", now=NOW)
    with pytest.raises(QueryError) as exc:
        query_activity(populated, SETTINGS, cursor=page["pagination"]["next_cursor"], **extra)
    assert exc.value.code == "invalid_parameter"


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "devices"),
        ("filters", {"device": MAC}),
        ("filters", []),
        ("filters", {"device": 123, "domain": DOMAIN}),
        ("limit", 2001),
        ("limit", True),
        ("snapshot", 2**63),
        ("timestamp", 0),
        ("id", 0),
        ("from", "2000-01-01T00:00:00Z"),
    ],
)
def test_general_cursor_validation(populated, field, value):
    token = query_activity(populated, SETTINGS, limit="1", now=NOW)["pagination"]["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    payload[field] = value
    changed = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(QueryError) as exc:
        query_activity(populated, SETTINGS, cursor=changed)
    assert exc.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "parameters",
    [
        {"from_time": iso(START), "to_time": iso(END)},
        {"to_time": iso(END)},
        {"from_time": iso(START)},
        {"from_time": "2026-09-10T08:00:00-04:00", "to_time": "2026-09-17T14:00:00+02:00"},
    ],
)
def test_new_and_existing_endpoints_share_time_rules(populated, parameters):
    old = query_device_activity(populated, SETTINGS, MAC, now=NOW, **parameters)
    new = query_activity(populated, SETTINGS, device=MAC, now=NOW, **parameters)
    assert new["range"] == old["range"]
    assert [{"timestamp": r["timestamp"], "domain": r["domain"]} for r in new["activity"]] == old[
        "activity"
    ]


def test_general_time_and_size_limits_apply(populated):
    with pytest.raises(QueryError) as exc:
        query_activity(populated, SETTINGS, from_time="2026-01-01T00:00:00Z", now=NOW)
    assert exc.value.code == "range_too_large"
    with pytest.raises(QueryError) as exc:
        query_activity(populated, SETTINGS, limit="2001", now=NOW)
    assert exc.value.code == "invalid_limit"
    with pytest.raises(QueryError) as exc:
        query_activity(populated, SETTINGS, from_time="2026-09-10", now=NOW)
    assert exc.value.code == "invalid_datetime"
    settings = replace(SETTINGS, api_default_page_size=1, api_default_window_days=2)
    result = query_activity(populated, settings, now=NOW)
    assert result["pagination"]["limit"] == 1
    assert result["range"]["from"] == iso(END - 2 * 86400)


def test_domain_summary_counts_all_history_duplicates_and_missing_macs(populated):
    before = populated.get_state()
    summary = get_domain_summary(populated, DOMAIN)
    assert summary["scope"] == "all_collected_history"
    assert summary["first_seen"] == iso(START - 40 * 86400)
    assert summary["last_seen"] == iso(END)
    assert summary["total_observation_count"] == 6
    assert summary["distinct_device_count"] == 2
    assert [d["mac"] for d in summary["devices"]] == [MAC, OTHER, None]
    assert [d["observation_count"] for d in summary["devices"]] == [3, 2, 1]
    assert summary["devices"][0]["friendly_name"] == "Living room TV"
    assert summary["devices"][0]["last_seen"] == iso(START)
    assert summary["devices"][-1]["friendly_name"] is None
    assert (
        sum(d["observation_count"] for d in summary["devices"])
        == summary["total_observation_count"]
    )
    assert populated.get_state() == before


def test_device_summary_counts_null_urls_but_not_as_distinct_domains(populated):
    summary = get_device_summary(populated, MAC)
    assert summary == {
        "mac": MAC,
        "friendly_name": "Living room TV",
        "notes": "Main screen",
        "scope": "all_collected_history",
        "first_seen": iso(START - 40 * 86400),
        "last_seen": iso(START + 4),
        "total_observation_count": 7,
        "distinct_domain_count": 4,
    }
    empty = get_device_summary(populated, "empty-device")
    assert empty["first_seen"] is None and empty["last_seen"] is None
    assert empty["total_observation_count"] == empty["distinct_domain_count"] == 0


def test_domain_with_only_missing_mac_and_empty_domain_values(repository):
    repository.commit_import([HistoryRow(None, 1, DOMAIN), HistoryRow("", 2, "")], 0, 2)
    summary = get_domain_summary(repository, DOMAIN)
    assert summary["total_observation_count"] == 1
    assert summary["distinct_device_count"] == 0
    assert summary["devices"][0]["mac"] is None
    assert get_domain_summary(repository, "")["distinct_device_count"] == 1


def test_missing_summaries_and_exact_domain_matching(populated):
    for domain in ("NETFLIX.COM", "' OR 1=1 --"):
        with pytest.raises(QueryError) as exc:
            get_domain_summary(populated, domain)
        assert exc.value.code == "domain_not_found"
    with pytest.raises(QueryError) as exc:
        get_device_summary(populated, "' OR 1=1 --")
    assert exc.value.code == "device_not_found"
    assert get_domain_summary(populated, FULL_URL)["total_observation_count"] == 1


@pytest.fixture
def client(populated):
    app = create_app(SETTINGS, populated, Mock())
    app.config["TESTING"] = True
    return app.test_client()


def test_http_endpoints_and_full_url_identifiers(client):
    response = client.get(
        "/api/activity",
        query_string={
            "device": MAC,
            "domain": DOMAIN,
            "from": iso(START),
            "to": iso(END),
        },
    )
    assert response.status_code == 200 and response.is_json
    assert len(response.json["activity"]) == 2
    assert client.get(f"/api/devices/{MAC}").json["total_observation_count"] == 7
    assert client.get("/api/domains/netflix.com").json["total_observation_count"] == 6
    full_url = client.get("/api/domains/" + quote(FULL_URL, safe=""))
    assert full_url.status_code == 200
    assert full_url.json["domain"] == FULL_URL


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/api/activity?mac=x", 400, "invalid_parameter"),
        ("/api/activity?domain=a&domain=b", 400, "invalid_parameter"),
        ("/api/activity?cursor=bad", 400, "invalid_cursor"),
        ("/api/activity?limit=0", 400, "invalid_parameter"),
        ("/api/domains/missing", 404, "domain_not_found"),
        ("/api/devices/missing", 404, "device_not_found"),
        ("/api/domains/netflix.com?from=2026-09-01", 400, "invalid_parameter"),
        (f"/api/devices/{MAC}?limit=2", 400, "invalid_parameter"),
    ],
)
def test_http_errors_are_structured(client, path, status, code):
    response = client.get(path)
    assert response.status_code == status
    assert response.json["error"]["code"] == code


def test_summary_names_follow_existing_html_edits(client):
    client.get("/devices")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/devices",
        data={"csrf_token": token, "mac": MAC, "friendly_name": "Updated TV", "notes": "Updated"},
    )
    assert response.status_code == 303
    assert client.get(f"/api/devices/{MAC}").json["friendly_name"] == "Updated TV"
    assert (
        client.get("/api/domains/netflix.com").json["devices"][0]["friendly_name"] == "Updated TV"
    )
    assert "Updated TV" in client.get("/devices").get_data(as_text=True)
