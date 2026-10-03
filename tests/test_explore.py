import re
from datetime import UTC, datetime
from html import unescape
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from network_history.config import Settings
from network_history.explore import ViewedPages, local_input
from network_history.models import HistoryRow
from network_history.queries import QueryError
from network_history.web import create_app


@pytest.fixture
def client(repository):
    app = create_app(
        Settings(timezone="America/New_York", api_default_page_size=2),
        repository,
        Mock(),
    )
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def observations(repository):
    first = int(datetime(2026, 9, 15, 12, tzinfo=UTC).timestamp())
    repository.commit_import(
        [
            HistoryRow("aa", first, "netflix.com"),
            HistoryRow("aa", first, "netflix.com"),
            HistoryRow("bb", first + 1, "netflix.com"),
            HistoryRow("aa", first + 2, "www.netflix.com"),
            HistoryRow(None, first + 3, None),
            HistoryRow("", first + 4, "empty.example"),
        ],
        0,
        first + 5,
    )
    repository.update_device("aa", "Living room", "Family TV")
    return first


def search(client, **overrides):
    args = {"from": "2026-09-15T08:00", "to": "2026-09-15T09:00"}
    args.update(overrides)
    return client.get("/explore", query_string=args)


def link(html, label):
    match = re.search(r'href="([^"]+)">' + label + r"</a>", html)
    return unescape(match[1]) if match else None


def body(response):
    return response.get_data(as_text=True)


def test_navigation_and_default_range(client, repository):
    now = int(datetime.now(UTC).timestamp())
    repository.commit_import(
        [
            HistoryRow("new", now - 60, "recent.example"),
            HistoryRow("old", now - 10 * 86400, "old.example"),
        ],
        0,
        now,
    )
    response = client.get("/explore")
    html = body(response)
    assert response.status_code == 200
    assert "recent.example" in html and "old.example" not in html
    assert 'value="mac:old"' in html  # Dropdown includes older devices.
    assert "America/New_York" in html
    assert 'name="from" value=""' not in html
    assert "Collector" in html and "Collect now" in html
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert 'href="/explore"' in body(client.get("/"))
    assert "Device Summary" in html and "Domain Lookup" in html


def test_combined_filters_and_exclusive_bounds(client, observations):
    response = search(client, device="mac:aa", domain="netflix.com")
    html = body(response)
    assert response.status_code == 200
    assert "2 observations on this page" in html
    assert html.count('<td class="url">netflix.com</td>') == 2
    assert "Living room" in html and "<code>aa</code>" in html
    assert "2026-09-15 08:00:00 EDT (-0400)" in html
    assert link(html, "Next") is None
    response = search(client, **{"from": "2026-09-15T07:59", "to": "2026-09-15T08:00"})
    assert "No matching activity" in body(response)


def test_pagination_preserves_snapshot_duplicates_and_previous(client, repository, observations):
    first = body(search(client))
    next_url = link(first, "Next")
    assert next_url and "cursor=" not in next_url
    # A late insert within the searched period must not enter the original snapshot.
    repository.commit_import(
        [HistoryRow("cc", observations + 100, "late.example")],
        observations + 4,
        observations + 101,
    )
    second = body(client.get(next_url))
    assert "www.netflix.com" in second
    previous_url = link(second, "Previous")
    previous = body(client.get(previous_url))
    assert (
        re.search(r"<tbody>(.*?)</tbody>", previous, re.S)[1]
        == re.search(r"<tbody>(.*?)</tbody>", first, re.S)[1]
    )
    assert link(previous, "Next") == next_url
    third = body(client.get(link(second, "Next")))
    assert "(missing MAC)" in third and "(missing domain)" in third
    assert "empty.example" in third and "late.example" not in third
    assert "End of results" in third and link(third, "Next") is None
    assert "6 observations" not in third


def test_summary_views_use_all_history_and_local_times(client, observations):
    domain = client.get(
        "/explore", query_string={"tab": "domain", "domain": "netflix.com", "search": "1"}
    )
    assert domain.status_code == 200
    html = body(domain)
    assert "All collected history" in html
    assert "<dd>3</dd>" in html and "<dd>2</dd>" in html
    assert "Living room" in html and "<code>bb</code>" in html
    assert "2026-09-15 08:00:00 EDT (-0400)" in html
    device = client.get(
        "/explore", query_string={"tab": "device", "device": "mac:aa", "search": "1"}
    )
    html = body(device)
    assert device.status_code == 200
    assert "Family TV" in html
    assert "<dd>3</dd>" in html and "<dd>2</dd>" in html
    assert "<code>aa</code>" in html


def test_empty_mac_is_distinct_from_all_devices(client, observations):
    html = body(search(client, device="mac:"))
    assert "empty.example" in html
    assert "1 observation on this page" in html
    assert '<td class="url">netflix.com</td>' not in html
    response = client.get(
        "/explore", query_string={"tab": "device", "device": "mac:", "search": "1"}
    )
    assert response.status_code == 200 and "(empty MAC)" in body(response)


@pytest.mark.parametrize(
    "args, expected, status",
    [
        ({"from": "garbage"}, "valid local date", 400),
        ({"from": "2026-09-16T09:00"}, "from must be earlier", 400),
        ({"from": "2026-01-01T00:00"}, "cannot exceed 31 days", 400),
        ({"from": "2026-03-08T02:30"}, "does not exist", 400),
        ({"from": "2026-11-01T01:30"}, "occurs twice", 400),
        ({"device": "bad"}, "Choose a device", 400),
        ({"domain": "unknown.example"}, "No matching activity", 200),
        ({"page": "expired"}, "Run Search again", 400),
    ],
)
def test_activity_errors_retain_input(client, args, expected, status):
    response = (
        search(client, domain="keep.example", **args)
        if "domain" not in args
        else search(client, **args)
    )
    assert response.status_code == status
    assert expected in body(response)
    if "domain" not in args:
        assert 'value="keep.example"' in body(response)


@pytest.mark.parametrize(
    "args, expected, status",
    [
        (
            {"tab": "domain", "domain": "unknown.example", "search": "1"},
            "No collected observations",
            404,
        ),
        ({"tab": "device", "device": "mac:unknown", "search": "1"}, "No known device", 404),
        ({"tab": "domain", "search": "1"}, "Enter a domain", 400),
        ({"tab": "device", "search": "1"}, "Choose a device", 400),
    ],
)
def test_summary_errors(client, args, expected, status):
    response = client.get("/explore", query_string=args)
    assert response.status_code == status and expected in body(response)


def test_pages_are_bounded_and_browser_scoped():
    pages = ViewedPages(capacity=2)
    first = pages.add("one", {"number": 1})
    second = pages.add("one", {"number": 2}, first)
    with pytest.raises(QueryError, match="no longer available"):
        pages.get(second, "two")
    pages.add("one", {"number": 3}, second)
    with pytest.raises(QueryError, match="no longer available"):
        pages.get(first, "one")
    assert pages.get(second, "one") == ({"number": 2}, first)


def test_page_link_cannot_be_used_by_another_browser(client, observations):
    page = link(body(search(client)), "Next")
    other = client.application.test_client()
    assert other.get(page).status_code == 400


def test_local_time_conversion():
    tz = ZoneInfo("America/New_York")
    assert local_input("2026-01-15T08:00", tz) == "2026-01-15T13:00:00+00:00"
    assert local_input("2026-07-15T08:00:01", tz) == "2026-07-15T12:00:01+00:00"
    with pytest.raises(QueryError):
        local_input("2026-07-15T08:00Z", tz)


def test_untrusted_fields_are_escaped_and_full_urls_match(client, repository):
    stamp = int(datetime(2026, 9, 15, 12, tzinfo=UTC).timestamp())
    raw = 'https://example.com/a?x=1#<script>alert("x")</script>'
    repository.commit_import([HistoryRow("aa", stamp, raw)], 0, stamp + 1)
    repository.update_device("aa", "<script>name</script>", "<script>notes</script>")
    for response in [
        search(client),
        client.get("/explore", query_string={"tab": "domain", "domain": raw, "search": "1"}),
        client.get("/explore", query_string={"tab": "device", "device": "mac:aa", "search": "1"}),
    ]:
        assert response.status_code == 200
        html = body(response)
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert 'href="https://example.com' not in html


def test_device_edit_remains_visible_in_new_exploration(client, observations):
    client.get("/devices")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/devices",
        data={
            "csrf_token": token,
            "mac": "aa",
            "friendly_name": "Renamed TV",
            "notes": "New notes",
        },
    )
    assert response.status_code == 303
    assert "Renamed TV" in body(search(client))
    assert "New notes" in body(
        client.get(
            "/explore",
            query_string={
                "tab": "device",
                "device": "mac:aa",
                "search": "1",
            },
        )
    )


def test_empty_summary_tabs_and_repeated_parameters(client):
    for tab in ("domain", "device"):
        response = client.get("/explore", query_string={"tab": tab})
        assert response.status_code == 200
        assert 'role="alert"' not in body(response)
    response = client.get("/explore?domain=one&domain=two")
    assert response.status_code == 400
    assert "must not be repeated" in body(response)
