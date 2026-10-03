"""Server-rendered exploration over the same application queries as the HTTP API."""

import secrets
from collections import OrderedDict
from datetime import UTC, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, session, url_for

from network_history.queries import (
    QueryError,
    get_device_summary,
    get_domain_summary,
    query_activity,
    query_devices,
)


def local_input(value: str, timezone: ZoneInfo) -> str:
    """Convert a wall-clock form value without guessing across DST transitions."""
    try:
        local = datetime.fromisoformat(value)
        if local.tzinfo is not None or "T" not in value:
            raise ValueError
        candidates = {
            local.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
            for fold in (0, 1)
            if local.replace(tzinfo=timezone, fold=fold)
            .astimezone(UTC)
            .astimezone(timezone)
            .replace(tzinfo=None)
            == local
        }
    except (ValueError, OverflowError) as exc:
        raise QueryError("invalid_datetime", "Enter a valid local date and time.") from exc
    if not candidates:
        raise QueryError(
            "invalid_datetime",
            "That local time does not exist because the clock moves forward. Choose another time.",
        )
    if len(candidates) > 1:
        raise QueryError(
            "invalid_datetime",
            "That local time occurs twice when the clock moves back. "
            "Choose a boundary before or after the repeated hour.",
        )
    return candidates.pop().isoformat()


def form_time(value: str, timezone: ZoneInfo) -> str:
    return datetime.fromisoformat(value).astimezone(timezone).strftime("%Y-%m-%dT%H:%M:%S")


class ViewedPages:
    """Bounded, process-local navigation; never stored in the history database."""

    def __init__(self, capacity=64):
        self.capacity = capacity
        self.pages = OrderedDict()
        self.lock = Lock()

    def add(self, owner, result, previous=None):
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.pages[token] = (owner, result, previous)
            while len(self.pages) > self.capacity:
                self.pages.popitem(last=False)
        return token

    def get(self, token, owner):
        with self.lock:
            page = self.pages.get(token)
            if page is None or page[0] != owner:
                raise QueryError(
                    "expired_page",
                    "This saved page is no longer available. Run Search again to start a new search.",
                )
            self.pages.move_to_end(token)
            return page[1], page[2]


def create_explore(settings, repository):
    explore = Blueprint("explore", __name__, url_prefix="/explore")
    timezone = ZoneInfo(settings.timezone)
    pages = ViewedPages()

    @explore.get("")
    def index():
        tab = request.args.get("tab", "activity")
        if tab not in {"activity", "domain", "device"}:
            tab = "activity"
        devices = query_devices(repository, settings, days="all")["devices"]
        # Prefix values so "all devices" remains distinct from a stored empty MAC.
        selected = request.args.get("device", "")
        domain = request.args.get("domain", "")
        start = request.args.get("from", "")
        end = request.args.get("to", "")
        result = None
        error = None
        previous_url = next_url = None
        status = 200
        try:
            if any(len(values) != 1 for _, values in request.args.lists()):
                raise QueryError("invalid_parameter", "Form parameters must not be repeated.")
            if tab == "activity":
                owner = session.setdefault("explore_owner", secrets.token_urlsafe(24))
                saved = request.args.get("page")
                advance = request.args.get("next")
                if saved and advance:
                    raise QueryError("invalid_parameter", "Choose one page navigation action.")
                if saved or advance:
                    result, previous = pages.get(saved or advance, owner)
                    token = saved
                    if advance:
                        cursor = result["pagination"]["next_cursor"]
                        if cursor is None:
                            raise QueryError("invalid_parameter", "There are no further results.")
                        result = query_activity(repository, settings, cursor=cursor)
                        previous = advance
                        token = pages.add(owner, result, previous)
                else:
                    if selected and not selected.startswith("mac:"):
                        raise QueryError("invalid_parameter", "Choose a device from the list.")
                    result = query_activity(
                        repository,
                        settings,
                        device=selected[4:] if selected else None,
                        domain=domain if domain else None,
                        from_time=local_input(start, timezone) if start else None,
                        to_time=local_input(end, timezone) if end else None,
                    )
                    previous = None
                    token = pages.add(owner, result)
                selected = (
                    "mac:" + result["filters"]["device"]
                    if result["filters"]["device"] is not None
                    else ""
                )
                domain = result["filters"]["domain"] or ""
                start = form_time(result["range"]["from"], timezone)
                end = form_time(result["range"]["to"], timezone)
                if previous:
                    previous_url = url_for("explore.index", page=previous)
                if result["pagination"]["has_more"]:
                    next_url = url_for("explore.index", next=token)
            elif tab == "domain" and "search" in request.args:
                if not domain:
                    raise QueryError("invalid_parameter", "Enter a domain or hostname.")
                result = get_domain_summary(repository, domain)
            elif tab == "device" and "search" in request.args:
                if not selected.startswith("mac:"):
                    raise QueryError("invalid_parameter", "Choose a device from the list.")
                result = get_device_summary(repository, selected[4:])
        except QueryError as exc:
            result = None
            error = str(exc)
            status = 404 if exc.code in {"domain_not_found", "device_not_found"} else 400
        return render_template(
            "explore.html",
            tab=tab,
            devices=devices,
            selected=selected,
            domain=domain,
            start=start,
            end=end,
            result=result,
            error=error,
            previous_url=previous_url,
            next_url=next_url,
            default_days=settings.api_default_window_days,
            max_days=settings.api_max_range_days,
        ), status

    return explore
