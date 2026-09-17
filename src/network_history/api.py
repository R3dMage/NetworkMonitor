"""Thin HTTP adapter for the reusable history query functions."""

from flask import Blueprint, jsonify, request

from network_history.config import Settings
from network_history.queries import QueryError, query_device_activity, query_devices
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
        status = 404 if error.code == "device_not_found" else 400
        return jsonify(error={"code": error.code, "message": str(error)}), status

    @api.get("/devices")
    def devices():
        args = parameters({"days"})
        return jsonify(query_devices(repository, settings, days=args.get("days")))

    @api.get("/devices/<mac>/activity")
    def activity(mac):
        args = parameters({"from", "to", "limit", "cursor"})
        return jsonify(
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

    return api
