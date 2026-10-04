"""Thin HTTP adapter for the reusable history query functions."""

from flask import Blueprint, jsonify, request

from network_history.config import Settings
from network_history.queries import (
    QueryError,
    get_device_summary,
    get_domain_summary,
    query_activity,
    query_device_activity,
    query_devices,
)
from network_history.request_log_web import log_query, query_response
from network_history.storage.repository import Repository


def create_api(settings: Settings, repository: Repository) -> Blueprint:
    api = Blueprint("api", __name__, url_prefix="/api")

    def parameters(allowed: set[str]) -> dict[str, str]:
        if set(request.args) - allowed:
            raise QueryError("invalid_parameter", "Unknown query parameter.")
        if any(len(values) != 1 for _, values in request.args.lists()):
            raise QueryError("invalid_parameter", "Query parameters must not be repeated.")
        return request.args.to_dict()

    @api.errorhandler(QueryError)
    def query_error(error):
        status = 404 if error.code in {"device_not_found", "domain_not_found"} else 400
        return jsonify(error={"code": error.code, "message": str(error)}), status

    @api.get("/devices")
    @log_query("list_devices")
    def devices():
        args = parameters({"days"})
        return query_response(query_devices(repository, settings, days=args.get("days")))

    @api.get("/devices/<mac>/activity")
    @log_query("get_device_activity")
    def activity(mac):
        args = parameters({"from", "to", "limit", "cursor"})
        return query_response(
            query_device_activity(
                repository,
                settings,
                mac,
                from_time=args.get("from"),
                to_time=args.get("to"),
                limit=args.get("limit"),
                cursor=args.get("cursor"),
            )
        )

    @api.get("/activity")
    @log_query("get_activity")
    def observations():
        args = parameters({"device", "domain", "from", "to", "limit", "cursor"})
        return query_response(
            query_activity(
                repository,
                settings,
                device=args.get("device"),
                domain=args.get("domain"),
                from_time=args.get("from"),
                to_time=args.get("to"),
                limit=args.get("limit"),
                cursor=args.get("cursor"),
            )
        )

    @api.get("/devices/<mac>")
    @log_query("get_device_summary")
    def device_summary(mac):
        parameters(set())
        return query_response(get_device_summary(repository, mac))

    @api.get("/domains/<path:domain>")
    @log_query("get_domain_summary")
    def domain_summary(domain):
        parameters(set())
        return query_response(get_domain_summary(repository, domain))

    return api
