"""HTTP instrumentation and the log viewer; database SQL lives in the log store."""

import json
import re
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, render_template, request


def log_query(operation):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            parameters = {
                key: values[0] if len(values) == 1 else values
                for key, values in request.args.lists()
            }
            path_parameters = request.view_args or {}
            submitted = parameters.copy() if parameters.keys() & path_parameters.keys() else None
            parameters.update(path_parameters)
            if submitted is not None:
                parameters["query_parameters"] = submitted
            scope = current_app.extensions["request_logger"].begin(
                source="http",
                operation=operation,
                parameters=parameters,
                http_method=request.method,
                http_path=request.path,
            )
            g.logged_operation = scope
            try:
                return view(*args, **kwargs)
            except Exception as exc:
                scope.fail(exc)
                raise

        return wrapped

    return decorate


def query_response(result):
    scope = getattr(g, "logged_operation", None)
    if scope is not None:
        scope.result(result)
    return jsonify(result)


def install_request_logging(app, logger):
    app.extensions["request_logger"] = logger

    @app.after_request
    def finish_log(response):
        scope = getattr(g, "logged_operation", None)
        if scope is not None:
            scope.finish(http_status=response.status_code)
        return response

    @app.teardown_request
    def failed_request(error):
        scope = getattr(g, "logged_operation", None)
        if scope is not None and not scope.finished:
            if error is not None:
                scope.fail(error)
            scope.finish(http_status=500)


def create_request_log_page(settings, logger):
    page = Blueprint("request_log", __name__)

    @page.get("/request-log")
    def index():
        entered = request.args.get("limit", "100")
        rows = []
        error = None
        status = 200
        digits = entered.lstrip("0") or "0"
        if (
            set(request.args) - {"limit"}
            or len(request.args.getlist("limit")) > 1
            or not re.fullmatch(r"[0-9]+", entered)
            or digits == "0"
        ):
            error = "Enter a positive whole number of calls."
            status = 400
        elif len(digits) > 5 or int(digits) > settings.request_log_max_display:
            error = f"Show at most {settings.request_log_max_display} calls."
            status = 400
        else:
            rows = logger.recent(int(digits))
            if rows is None:
                error = "Request logs are temporarily unavailable. Try again shortly."
                rows = []
                status = 503
        for entry in rows:
            entry["parameter_items"] = [
                (name, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                for name, value in json.loads(entry["parameters_json"]).items()
            ]
        return render_template(
            "request_log.html",
            entries=rows,
            entered=entered,
            error=error,
            log_warning=logger.warning,
            maximum=settings.request_log_max_display,
        ), status

    return page
