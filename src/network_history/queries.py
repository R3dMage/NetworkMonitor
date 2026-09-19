"""Reusable read-only queries. No HTTP framework or database-specific SQL."""

import base64
import binascii
import json
import re
from datetime import UTC, datetime, timedelta

from network_history.config import Settings
from network_history.models import HistoryPage
from network_history.storage.repository import Repository

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ISO_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
MAX_ID = 2**63 - 1


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _parse_datetime(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not ISO_DATETIME.fullmatch(value):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise QueryError(
            "invalid_datetime",
            "Use an ISO-8601 datetime with seconds and a timezone, such as 2026-09-01T00:00:00Z.",
        ) from exc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: int | None) -> str | None:
    return _iso(EPOCH + timedelta(seconds=value)) if value is not None else None


def _second_bound(value: datetime) -> int:
    # Integer source timestamps: ceil both bounds preserves [from, to) exactly,
    # including fractional seconds, without floating-point rounding.
    delta = value - EPOCH
    return delta.days * 86400 + delta.seconds + bool(delta.microseconds)


def _positive_integer(value: str, name: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise QueryError("invalid_parameter", f"{name} must be a positive integer.")
    result = int(value)
    if result < 1:
        raise QueryError("invalid_parameter", f"{name} must be a positive integer.")
    return result


def _check_range(start: datetime, end: datetime, settings: Settings) -> None:
    if start >= end:
        raise QueryError("invalid_range", "from must be earlier than to.")
    if (end - start).total_seconds() > settings.api_max_range_days * 86400:
        raise QueryError(
            "range_too_large",
            f"The activity range cannot exceed {settings.api_max_range_days} days.",
        )


def _range_json(start: datetime, end: datetime) -> dict:
    return {"from": _iso(start), "to": _iso(end)}


def _encode_cursor(payload: dict) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode_cursor(
    cursor: str,
    mac: str | None,
    settings: Settings,
    *,
    general: bool = False,
):
    """Keep v1 device cursors valid; v2 identifies the general activity endpoint."""
    try:
        if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", cursor):
            raise ValueError
        payload = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        common = {"v", "from", "to", "limit", "snapshot", "timestamp", "id"}
        identity = {"scope", "filters"} if general else {"mac"}
        if not isinstance(payload, dict) or set(payload) != common | identity:
            raise ValueError
        if type(payload["v"]) is not int or payload["v"] != (2 if general else 1):
            raise ValueError
        filters = {"device": mac, "domain": None}
        if general:
            if payload["scope"] != "activity":
                raise ValueError
            filters = payload["filters"]
            if not isinstance(filters, dict) or set(filters) != {"device", "domain"}:
                raise ValueError
            if any(value is not None and not isinstance(value, str) for value in filters.values()):
                raise ValueError
        elif payload["mac"] != mac:
            raise ValueError
        for key in ("limit", "snapshot", "timestamp", "id"):
            if type(payload[key]) is not int:
                raise ValueError
        if not 1 <= payload["limit"] <= settings.api_max_page_size:
            raise ValueError
        if not 1 <= payload["id"] <= payload["snapshot"] <= MAX_ID:
            raise ValueError
        start, end = _parse_datetime(payload["from"]), _parse_datetime(payload["to"])
        _check_range(start, end, settings)
        if not _second_bound(start) <= payload["timestamp"] < _second_bound(end):
            raise ValueError
        return (
            start,
            end,
            payload["limit"],
            payload["snapshot"],
            (payload["timestamp"], payload["id"]),
            filters,
        )
    except (ValueError, TypeError, OverflowError, binascii.Error, RecursionError) as exc:
        raise QueryError(
            "invalid_cursor", "Invalid cursor for this endpoint or the current API limits."
        ) from exc


def _initial_page(
    settings: Settings,
    from_time: str | None,
    to_time: str | None,
    limit: str | None,
    now: datetime | None,
) -> tuple[datetime, datetime, int]:
    end = (
        _parse_datetime(to_time)
        if to_time is not None
        else now
        if now is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    try:
        start = (
            _parse_datetime(from_time)
            if from_time is not None
            else end - timedelta(days=settings.api_default_window_days)
        )
    except OverflowError as exc:
        raise QueryError("invalid_range", "The default window exceeds datetime bounds.") from exc
    _check_range(start, end, settings)
    page_size = (
        settings.api_default_page_size if limit is None else _positive_integer(limit, "limit")
    )
    if page_size > settings.api_max_page_size:
        raise QueryError("invalid_limit", f"limit cannot exceed {settings.api_max_page_size}.")
    return start, end, page_size


def _pagination(
    page: HistoryPage,
    size: int,
    start: datetime,
    end: datetime,
    identity: dict,
) -> dict:
    has_more = len(page.rows) > size
    next_cursor = None
    if has_more:
        last = page.rows[size - 1]
        next_cursor = _encode_cursor(
            {
                **identity,
                "from": _iso(start),
                "to": _iso(end),
                "limit": size,
                "snapshot": page.snapshot_id,
                "timestamp": last.timestamp,
                "id": last.id,
            }
        )
        if len(next_cursor) > 4096:
            raise QueryError("invalid_parameter", "Filters are too long for a pagination cursor.")
    return {"limit": size, "has_more": has_more, "next_cursor": next_cursor}


def query_devices(
    repository: Repository,
    settings: Settings,
    *,
    days: str | None = None,
    now: datetime | None = None,
) -> dict:
    effective_range = None
    if days == "all":
        devices = repository.list_devices()
    else:
        lookback = (
            settings.api_default_window_days if days is None else _positive_integer(days, "days")
        )
        end = now if now is not None else datetime.now(UTC).replace(microsecond=0)
        try:
            start = end - timedelta(days=lookback)
        except OverflowError as exc:
            raise QueryError("invalid_range", "days exceeds the supported datetime range.") from exc
        devices = repository.devices_seen_between(_second_bound(start), _second_bound(end))
        effective_range = _range_json(start, end)
    return {
        "range": effective_range,
        "devices": [
            {"mac": d.mac, "friendly_name": d.friendly_name or None, "notes": d.notes}
            for d in devices
        ],
    }


def query_device_activity(
    repository: Repository,
    settings: Settings,
    mac: str,
    *,
    from_time: str | None = None,
    to_time: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Existing response shape and v1 cursor contract remain supported."""
    after = snapshot = None
    if cursor is not None:
        if any(value is not None for value in (from_time, to_time, limit)):
            raise QueryError("invalid_parameter", "Use cursor alone for continuation requests.")
        start, end, size, snapshot, after, _ = _decode_cursor(cursor, mac, settings)
    else:
        start, end, size = _initial_page(settings, from_time, to_time, limit, now)
    device = repository.find_device(mac)
    if device is None:
        raise QueryError("device_not_found", "No known device has this MAC address.")
    page = repository.activity_page(
        mac,
        _second_bound(start),
        _second_bound(end),
        size + 1,
        after=after,
        snapshot_id=snapshot,
    )
    return {
        "device": {"mac": device.mac, "friendly_name": device.friendly_name or None},
        "range": _range_json(start, end),
        "activity": [
            {"timestamp": _timestamp(row.timestamp), "domain": row.url} for row in page.rows[:size]
        ],
        "pagination": _pagination(page, size, start, end, {"v": 1, "mac": mac}),
    }


def query_activity(
    repository: Repository,
    settings: Settings,
    *,
    device: str | None = None,
    domain: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict:
    after = snapshot = None
    filters = {"device": device, "domain": domain}
    if cursor is not None:
        if any(value is not None for value in (device, domain, from_time, to_time, limit)):
            raise QueryError("invalid_parameter", "Use cursor alone for continuation requests.")
        start, end, size, snapshot, after, filters = _decode_cursor(
            cursor, None, settings, general=True
        )
    else:
        if any(value is not None and not isinstance(value, str) for value in filters.values()):
            raise QueryError("invalid_parameter", "device and domain must be strings.")
        start, end, size = _initial_page(settings, from_time, to_time, limit, now)
    page = repository.activity_page(
        filters["device"],
        _second_bound(start),
        _second_bound(end),
        size + 1,
        domain=filters["domain"],
        after=after,
        snapshot_id=snapshot,
    )
    return {
        "filters": filters,
        "range": _range_json(start, end),
        "activity": [
            {
                "timestamp": _timestamp(row.timestamp),
                "mac": row.mac,
                "friendly_name": row.friendly_name or None,
                "domain": row.url,
            }
            for row in page.rows[:size]
        ],
        "pagination": _pagination(
            page, size, start, end, {"v": 2, "scope": "activity", "filters": filters}
        ),
    }


def get_device_summary(repository: Repository, mac: str) -> dict:
    summary = repository.device_summary(mac)
    if summary is None:
        raise QueryError("device_not_found", "No known device has this MAC address.")
    return {
        "mac": summary.mac,
        "friendly_name": summary.friendly_name or None,
        "notes": summary.notes,
        "scope": "all_collected_history",
        "first_seen": _timestamp(summary.first_seen),
        "last_seen": _timestamp(summary.last_seen),
        "total_observation_count": summary.total_observation_count,
        "distinct_domain_count": summary.distinct_domain_count,
    }


def get_domain_summary(repository: Repository, domain: str) -> dict:
    groups = repository.domain_device_summaries(domain)
    if not groups:
        raise QueryError("domain_not_found", "No collected observations match this domain.")
    return {
        "domain": domain,
        "scope": "all_collected_history",
        "first_seen": _timestamp(min(group.first_seen for group in groups)),
        "last_seen": _timestamp(max(group.last_seen for group in groups)),
        "total_observation_count": sum(group.observation_count for group in groups),
        "distinct_device_count": sum(group.mac is not None for group in groups),
        "devices": [
            {
                "mac": group.mac,
                "friendly_name": group.friendly_name or None,
                "first_seen": _timestamp(group.first_seen),
                "last_seen": _timestamp(group.last_seen),
                "observation_count": group.observation_count,
            }
            for group in groups
        ],
    }
