import logging
import signal
import threading
from collections.abc import Callable

from prometheus_client import start_http_server

logger = logging.getLogger(__name__)


def run_forever(name: str, step: Callable[[], bool], interval_seconds: float, metrics_port: int = 0) -> None:
    """Call `step` until SIGINT/SIGTERM. `step` returns True when it did work, in
    which case it is called again immediately instead of sleeping."""
    if metrics_port:
        try:
            start_http_server(metrics_port)
        except OSError:
            logger.warning("Metrics port %s unavailable", metrics_port)
    stop = threading.Event()

    def _stop(*_args):
        logger.info("%s stopping", name)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("%s started", name)

    failures = 0
    while not stop.is_set():
        try:
            busy = step()
            failures = 0
        except Exception:
            failures += 1
            logger.exception("%s iteration failed", name)
            busy = False
        if not busy:
            stop.wait(min(interval_seconds * (2 ** min(failures, 5)), 30.0))
