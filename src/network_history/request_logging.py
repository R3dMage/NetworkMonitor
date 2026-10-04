"""Caller-independent operation logging. No HTTP framework or response persistence."""

import json
import logging
import time
from contextlib import contextmanager
from threading import Lock

from network_history.queries import QueryError
from network_history.storage.request_log import RequestLogStore, SQLiteRequestLog

LOG = logging.getLogger(__name__)


def result_metadata(operation, result):
    if not isinstance(result, dict):
        return None, None
    if operation in {"get_activity", "get_device_activity"}:
        count = len(result["activity"])
    elif operation == "list_devices":
        count = len(result["devices"])
    elif operation in {"get_device_summary", "get_domain_summary"}:
        count = 1
    else:
        count = None
    effective = {}
    for container in ("range", "filters"):
        if isinstance(result.get(container), dict):
            effective.update(result[container])
    if isinstance(result.get("pagination"), dict):
        effective["limit"] = result["pagination"]["limit"]
    if isinstance(result.get("device"), dict):
        effective["device"] = result["device"].get("mac")
    for field in ("mac", "domain"):
        if field in result:
            effective[field] = result[field]
    return count, effective or None


class Operation:
    def __init__(self, logger, source, operation, parameters, http_method, http_path):
        self.logger = logger
        self.started = time.perf_counter()
        self.record = {
            "started_at_ms": time.time_ns() // 1_000_000,
            "source": source,
            "operation": operation,
            "parameters_json": json.dumps(parameters),
            "http_method": http_method,
            "http_path": http_path,
        }
        self.count = None
        self.effective = None
        self.error = None
        self.finished = False

    def result(self, result):
        try:
            self.count, self.effective = result_metadata(self.record["operation"], result)
        except Exception:
            # Metadata inspection must not alter the caller's result.
            self.count, self.effective = None, None
        return result

    def fail(self, error):
        if isinstance(error, QueryError):
            self.error = (error.code, str(error))
        else:
            self.error = ("internal_error", f"Query failed ({type(error).__name__}).")

    def finish(self, *, http_status=None):
        if self.finished:
            return
        self.finished = True
        success = self.error is None and (http_status is None or http_status < 400)
        error = self.error or (
            (None, None) if success else ("request_failed", "The request could not be completed.")
        )
        record = {
            **self.record,
            "success": int(success),
            "http_status": http_status,
            "result_count": self.count if success else None,
            "effective_parameters_json": json.dumps(self.effective) if self.effective else None,
            "duration_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "error_code": error[0],
            "error_message": error[1],
        }
        self.logger.write(record)


class RequestLogger:
    def __init__(self, store: RequestLogStore, *, cooldown=30):
        self.store = store
        self.cooldown = cooldown
        self.retry_at = 0.0
        self.initialized = False
        self.warning = None
        self.lock = Lock()

    def _attempt(self, action):
        # Bound contention independently of the SQLite busy timeout.
        if not self.lock.acquire(timeout=0.025):
            self.warning = "Request logging is busy; some calls may not be recorded."
            now = time.monotonic()
            if now >= self.retry_at:
                self.retry_at = now + self.cooldown
                LOG.warning("Request log busy; retrying on a later request")
            return None
        try:
            if time.monotonic() < self.retry_at:
                return None
            try:
                if not self.initialized:
                    self.store.initialize()
                    self.initialized = True
                result = action()
                self.warning = None
                return result
            except Exception as exc:
                self.initialized = False
                self.retry_at = time.monotonic() + self.cooldown
                self.warning = "Request logging is unavailable; some calls may not be recorded."
                # Never include raw exceptions, SQL, request data, or configuration.
                LOG.warning(
                    "Request log unavailable (%s); retrying on a later request", type(exc).__name__
                )
                return None
        finally:
            self.lock.release()

    def initialize(self):
        self._attempt(lambda: None)

    def write(self, record):
        self._attempt(lambda: self.store.append(record))

    def recent(self, limit):
        return self._attempt(lambda: self.store.recent(limit))

    def begin(self, *, source, operation, parameters=None, http_method=None, http_path=None):
        return Operation(self, source, operation, parameters or {}, http_method, http_path)

    @contextmanager
    def operation(self, *, source, operation, parameters=None):
        """For internal callers (or future MCP): scope.result(query(...))."""
        scope = self.begin(source=source, operation=operation, parameters=parameters)
        try:
            yield scope
        except Exception as exc:
            scope.fail(exc)
            raise
        finally:
            scope.finish()


def create_request_logger(settings):
    logger = RequestLogger(SQLiteRequestLog(settings.request_log_path, settings.database_path))
    logger.initialize()
    return logger
