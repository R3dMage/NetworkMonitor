import logging
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


class ManualCollector:
    """Owns only user-requested children; has no recurring timer or scheduling logic."""

    def __init__(self):
        self._mutex = threading.Lock()
        self._child: subprocess.Popen | None = None

    def start(self) -> bool:
        with self._mutex:
            if self._child is not None and self._child.poll() is None:
                return False
            child = subprocess.Popen(
                [sys.executable, "-m", "network_history", "collect-once", "--trigger", "web"],
                stdin=subprocess.DEVNULL,
                # Inherit stdout/stderr so manual runs appear in the web container logs.
            )
            self._child = child
            threading.Thread(target=self._reap, args=(child,), daemon=True).start()
            return True

    def _reap(self, child: subprocess.Popen) -> None:
        code = child.wait()
        logger.info("manual collection process exited code=%s", code)
        with self._mutex:
            if self._child is child:
                self._child = None

    def close(self) -> None:
        with self._mutex:
            child = self._child
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
