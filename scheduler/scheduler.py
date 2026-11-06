"""Recovery process: `python -m scheduler.scheduler`

Delayed work (scheduled_at, retry backoff) needs no polling here: it is carried by
outbox events whose available_at the dispatcher honours. This process repairs
everything that can go wrong in between.
"""
import logging
import time

from core.config import get_settings
from core.logging import configure_logging
from database import SessionLocal
from jobs.service import recovery_service
from scheduler.loop import run_forever
from taskqueue.redis_client import get_redis

logger = logging.getLogger(__name__)

PURGE_EVERY_SECONDS = 3600


def main() -> None:
    configure_logging()
    settings = get_settings()
    client = get_redis()
    last_purge = 0.0

    def step() -> bool:
        nonlocal last_purge
        recovery_service.requeue_orphaned_messages(client)
        recovery_service.recover_expired_leases(SessionLocal)
        recovery_service.redispatch_stale_queued(SessionLocal, settings.queued_redispatch_after_seconds)
        if time.monotonic() - last_purge > PURGE_EVERY_SECONDS:
            removed = recovery_service.purge_published_outbox(SessionLocal, settings.outbox_retention_days)
            if removed:
                logger.info("Purged %d published outbox events", removed)
            last_purge = time.monotonic()
        return False

    run_forever("scheduler", step, settings.scheduler_interval_seconds, settings.metrics_port)


if __name__ == "__main__":
    main()
