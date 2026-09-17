import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from network_history.config import ConfigurationError, Settings
from network_history.models import HistoryRow
from network_history.queries import QueryError, query_device_activity, query_devices

NOW = datetime(2026, 9, 16, 16, tzinfo=UTC)
MAC = "AA:BB:CC:DD:EE:01"
SETTINGS = Settings()
START = "2026-09-09T16:00:00Z"
END = "2026-09-16T16:00:00Z"


def seed(repository):
    start = int((NOW - timedelta(days=7)).timestamp())
    end = int(NOW.timestamp())
    repository.commit_import(
        [
            HistoryRow("old", start - 1, "old.example"),
            HistoryRow(MAC, start, "boundary.example"),
            HistoryRow(MAC, start, "boundary.example"),
            HistoryRow(MAC, start + 1, "https://example.com/path?raw=1"),
            HistoryRow(MAC, end - 1, None),
            HistoryRow("at-end", end, "outside.example"),
        ],
        0,
        end,
    )
    repository.update_device(MAC, "Office laptop", "Work")
    return start, end


def test_device_default_lookback_expanded_and_all(repository):
    seed(repository)
    result = query_devices(repository, SETTINGS, now=NOW)
    assert result["range"] == {"from": START, "to": END}
    assert result["devices"] == [{"mac": MAC, "friendly_name": "Office laptop", "notes": "Work"}]
    expanded = query_devices(repository, SETTINGS, days="30", now=NOW)
    assert {d["mac"] for d in expanded["devices"]} == {MAC, "old"}
    all_devices = query_devices(repository, SETTINGS, days="all", now=NOW)
    assert all_devices["range"] is None
    assert {d["mac"] for d in all_devices["devices"]} == {MAC, "old", "at-end"}
    assert next(d for d in all_devices["devices"] if d["mac"] == "old") == {
        "mac": "old",
        "friendly_name": None,
        "notes": None,
    }


def test_default_activity_boundaries_raw_domain_nulls_and_duplicates(repository):
    seed(repository)
    result = query_device_activity(repository, SETTINGS, MAC, now=NOW)
    assert result["range"] == {"from": START, "to": END}
    assert result["device"] == {"mac": MAC, "friendly_name": "Office laptop"}
    assert len(result["activity"]) == 4
    assert result["activity"][0] == result["activity"][1]
    assert result["activity"][0]["timestamp"] == START
    assert result["activity"][2]["domain"] == "https://example.com/path?raw=1"
    assert result["activity"][3]["domain"] is None
    assert result["pagination"] == {"limit": 500, "has_more": False, "next_cursor": None}


@pytest.mark.parametrize(
    "parameters,expected",
    [
        ({}, {"from": START, "to": END}),
        ({"to_time": END}, {"from": START, "to": END}),
        ({"from_time": START}, {"from": START, "to": END}),
        (
            {"from_time": "2026-09-09T12:00:00-04:00", "to_time": "2026-09-16T18:00:00+02:00"},
            {"from": START, "to": END},
        ),
    ],
)
def test_time_defaults_and_offsets(repository, parameters, expected):
    seed(repository)
    result = query_device_activity(repository, SETTINGS, MAC, now=NOW, **parameters)
    assert result["range"] == expected
    assert len(result["activity"]) == 4


def test_fractional_boundaries_are_not_rounded_to_include_wrong_rows(repository):
    seed(repository)
    result = query_device_activity(
        repository,
        SETTINGS,
        MAC,
        from_time="2026-09-09T16:00:00.000001Z",
        to_time="2026-09-09T16:00:01.000001Z",
    )
    assert [r["domain"] for r in result["activity"]] == ["https://example.com/path?raw=1"]


def test_paging_identical_timestamps_preserves_every_row_and_excludes_new_imports(repository):
    timestamp = int((NOW - timedelta(days=1)).timestamp())
    repository.commit_import([HistoryRow(MAC, timestamp, "same")] * 5, 0, timestamp)
    first = query_device_activity(repository, SETTINGS, MAC, limit="2", now=NOW)
    repository.commit_import(
        [HistoryRow(MAC, timestamp + 1, "later import")], timestamp, timestamp + 1
    )
    pages = [first]
    while pages[-1]["pagination"]["has_more"]:
        pages.append(
            query_device_activity(
                repository,
                SETTINGS,
                MAC,
                cursor=pages[-1]["pagination"]["next_cursor"],
                now=NOW + timedelta(days=10),
            )
        )
    rows = [row for page in pages for row in page["activity"]]
    assert len(pages) == 3
    assert len(rows) == 5
    assert all(row["domain"] == "same" for row in rows)
    assert all(page["range"] == first["range"] for page in pages)
    assert pages[-1]["pagination"]["next_cursor"] is None
    assert len(query_device_activity(repository, SETTINGS, MAC, now=NOW)["activity"]) == 6


def test_exact_full_page_has_no_false_continuation(repository):
    timestamp = int((NOW - timedelta(days=1)).timestamp())
    repository.commit_import([HistoryRow(MAC, timestamp, "x")] * 2, 0, timestamp)
    result = query_device_activity(repository, SETTINGS, MAC, limit="2", now=NOW)
    assert result["pagination"] == {"limit": 2, "has_more": False, "next_cursor": None}


@pytest.mark.parametrize(
    "parameters,code",
    [
        ({"from_time": "2026-09-01"}, "invalid_datetime"),
        ({"from_time": "2026-09-01T00:00:00"}, "invalid_datetime"),
        ({"from_time": "2026-02-30T00:00:00Z"}, "invalid_datetime"),
        ({"from_time": "2026-09-01T00:00:00+00:60"}, "invalid_datetime"),
        ({"from_time": "2026-09-01T00:00:00.1234567Z"}, "invalid_datetime"),
        ({"from_time": END, "to_time": START}, "invalid_range"),
        ({"from_time": END, "to_time": END}, "invalid_range"),
        ({"from_time": "2026-08-01T00:00:00Z", "to_time": END}, "range_too_large"),
        ({"limit": "2001"}, "invalid_limit"),
        ({"limit": "0"}, "invalid_parameter"),
        ({"limit": "-1"}, "invalid_parameter"),
        ({"limit": "1.0"}, "invalid_parameter"),
        ({"cursor": ""}, "invalid_cursor"),
        ({"cursor": "!"}, "invalid_cursor"),
        ({"cursor": "a" * 4097}, "invalid_cursor"),
        ({"cursor": "abc", "limit": "1"}, "invalid_parameter"),
    ],
)
def test_query_validation(repository, parameters, code):
    with pytest.raises(QueryError) as exc:
        query_device_activity(repository, SETTINGS, MAC, now=NOW, **parameters)
    assert exc.value.code == code


@pytest.mark.parametrize("days", ["0", "-1", "", "1.5", "ALL", "9999999999"])
def test_invalid_device_days(repository, days):
    with pytest.raises(QueryError):
        query_devices(repository, SETTINGS, days=days, now=NOW)


@pytest.mark.parametrize(
    "key,value",
    [
        ("limit", 2001),
        ("limit", True),
        ("snapshot", 2**63),
        ("id", 0),
        ("timestamp", -1),
        ("v", 2),
        ("mac", "other"),
        ("from", "2020-01-01T00:00:00Z"),
        ("to", None),
    ],
)
def test_cursor_contents_cannot_bypass_validation(repository, key, value):
    seed(repository)
    page = query_device_activity(repository, SETTINGS, MAC, limit="1", now=NOW)
    token = page["pagination"]["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    payload[key] = value
    changed = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(QueryError) as exc:
        query_device_activity(repository, SETTINGS, MAC, cursor=changed)
    assert exc.value.code == "invalid_cursor"


def test_cursor_respects_new_lower_limits(repository):
    seed(repository)
    page = query_device_activity(repository, SETTINGS, MAC, limit="2", now=NOW)
    with pytest.raises(QueryError, match="current API limits"):
        query_device_activity(
            repository,
            replace(SETTINGS, api_max_page_size=1),
            MAC,
            cursor=page["pagination"]["next_cursor"],
        )


def test_unknown_device_and_known_device_without_matching_rows(repository):
    seed(repository)
    empty = query_device_activity(repository, SETTINGS, "old", now=NOW)
    assert empty["activity"] == []
    assert empty["pagination"]["has_more"] is False
    with pytest.raises(QueryError) as exc:
        query_device_activity(repository, SETTINGS, "missing", now=NOW)
    assert exc.value.code == "device_not_found"


def test_mac_lookup_is_parameterized(repository):
    seed(repository)
    with pytest.raises(QueryError) as exc:
        query_device_activity(repository, SETTINGS, "' OR 1=1 --", now=NOW)
    assert exc.value.code == "device_not_found"


def test_configurable_defaults_and_bounds(repository):
    seed(repository)
    settings = replace(
        SETTINGS, api_default_window_days=2, api_default_page_size=1, api_max_range_days=7
    )
    result = query_device_activity(repository, settings, MAC, now=NOW)
    assert result["range"]["from"] == "2026-09-14T16:00:00Z"
    assert result["pagination"]["limit"] == 1
    with pytest.raises(QueryError, match="7 days"):
        query_device_activity(repository, settings, MAC, from_time="2026-09-01T00:00:00Z", now=NOW)


@pytest.mark.parametrize(
    "name,value",
    [
        ("API_DEFAULT_WINDOW_DAYS", "0"),
        ("API_MAX_RANGE_DAYS", "6"),
        ("API_DEFAULT_PAGE_SIZE", "2001"),
        ("API_MAX_PAGE_SIZE", "0"),
    ],
)
def test_invalid_api_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_api_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("API_DEFAULT_WINDOW_DAYS", "3")
    monkeypatch.setenv("API_MAX_RANGE_DAYS", "10")
    monkeypatch.setenv("API_DEFAULT_PAGE_SIZE", "20")
    monkeypatch.setenv("API_MAX_PAGE_SIZE", "100")
    settings = Settings.from_env()
    assert (
        settings.api_default_window_days,
        settings.api_max_range_days,
        settings.api_default_page_size,
        settings.api_max_page_size,
    ) == (3, 10, 20, 100)
