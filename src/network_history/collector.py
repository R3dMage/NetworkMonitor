import logging
import time
from collections.abc import Callable
from typing import Protocol

from network_history.models import HistoryRow
from network_history.storage.repository import Repository

logger = logging.getLogger(__name__)


class Source(Protocol):
    def fetch(self, checkpoint: int, cutoff: int) -> list[HistoryRow]: ...


def collect_once(
    repository: Repository,
    source: Source,
    safety_delay: int,
    trigger: str = "manual",
    clock: Callable[[], float] = time.time,
) -> str:
    """Exactly one pass. There is no schedule, retry loop or sleep in this function."""
    started = time.monotonic()
    with repository.collection_lock() as acquired:
        if not acquired:
            logger.info("collection skipped trigger=%s reason=already_running", trigger)
            return "skipped"
        try:
            state = repository.get_state()
            cutoff = int(clock()) - safety_delay
            logger.info(
                "collection started trigger=%s checkpoint=%s cutoff=%s",
                trigger,
                state.last_imported_timestamp,
                cutoff,
            )
            rows = source.fetch(state.last_imported_timestamp, cutoff)
            if any(not state.last_imported_timestamp < row.timestamp <= cutoff for row in rows):
                raise ValueError("Source returned rows outside the requested window")
            repository.commit_import(rows, state.last_imported_timestamp, int(clock()))
            logger.info(
                "collection succeeded trigger=%s rows=%s checkpoint=%s duration_seconds=%.3f",
                trigger,
                len(rows),
                max((row.timestamp for row in rows), default=state.last_imported_timestamp),
                time.monotonic() - started,
            )
            return "success"
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.error(
                "collection failed trigger=%s duration_seconds=%.3f error=%s",
                trigger,
                time.monotonic() - started,
                message,
            )
            try:
                repository.record_error(message)
            except Exception:
                logger.exception("Could not persist collection error")
            return "failed"
