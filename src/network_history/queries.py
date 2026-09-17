"""Reusable read-only queries. No HTTP framework or database-specific SQL."""

import base64
import binascii
import json
import re
from datetime import UTC, datetime, timedelta

from network_history.config import Settings
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


def _decode_cursor(cursor: str, mac: str, settings: Settings):
    try:
        if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", cursor):
            raise ValueError
        payload = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        keys = {"v", "mac", "from", "to", "limit", "snapshot", "timestamp", "id"}
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError
        if type(payload["v"]) is not int or payload["v"] != 1 or payload["mac"] != mac:
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
        )
    except (ValueError, TypeError, OverflowError, binascii.Error, RecursionError) as exc:
        raise QueryError(
            "invalid_cursor", "Invalid cursor for this device or the current API limits."
        ) from exc


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
    after = None
    snapshot = None
    if cursor is not None:
        if any(value is not None for value in (from_time, to_time, limit)):
            raise QueryError("invalid_parameter", "Use cursor alone for continuation requests.")
        start, end, page_size, snapshot, after = _decode_cursor(cursor, mac, settings)
    else:
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
            raise QueryError(
                "invalid_range", "The default window exceeds datetime bounds."
            ) from exc
        _check_range(start, end, settings)
        page_size = (
            settings.api_default_page_size if limit is None else _positive_integer(limit, "limit")
        )
        if page_size > settings.api_max_page_size:
            raise QueryError("invalid_limit", f"limit cannot exceed {settings.api_max_page_size}.")
    device = repository.find_device(mac)
    if device is None:
        raise QueryError("device_not_found", "No known device has this MAC address.")
    page = repository.activity_page(
        mac,
        _second_bound(start),
        _second_bound(end),
        page_size + 1,
        after=after,
        snapshot_id=snapshot,
    )
    has_more = len(page.rows) > page_size
    rows = page.rows[:page_size]
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "mac": mac,
                "from": _iso(start),
                "to": _iso(end),
                "limit": page_size,
                "snapshot": page.snapshot_id,
                "timestamp": last.timestamp,
                "id": last.id,
            }
        )
    return {
        "device": {"mac": device.mac, "friendly_name": device.friendly_name or None},
        "range": _range_json(start, end),
        "activity": [
            {"timestamp": _iso(EPOCH + timedelta(seconds=row.timestamp)), "domain": row.url}
            for row in rows
        ],
        "pagination": {
            "limit": page_size,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }
