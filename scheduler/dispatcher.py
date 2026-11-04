"""Outbox publisher process: `python -m scheduler.dispatcher`

Safe to run several replicas: rows are claimed with SELECT ... FOR UPDATE SKIP LOCKED
on PostgreSQL, and any duplicate publication is absorbed by the worker's atomic claim.
"""
from core.config import get_settings
from core.logging import configure_logging
from database import SessionLocal
from jobs.service.dispatch_service import dispatch_once
from scheduler.loop import run_forever
from taskqueue.redis_client import get_redis


def main() -> None:
    configure_logging()
    settings = get_settings()
    client = get_redis()

    def step() -> bool:
        return dispatch_once(SessionLocal, client, settings.dispatcher_batch_size) >= settings.dispatcher_batch_size

    run_forever("dispatcher", step, settings.dispatcher_interval_seconds, settings.metrics_port)


if __name__ == "__main__":
    main()
