"""Transactional outbox -> Redis.

1. Lock a batch of due, unpublished events (SKIP LOCKED on PostgreSQL) and move
   their jobs PENDING/RETRY_WAIT -> QUEUED. Commit.
2. Push the job ids to Redis.
3. Mark the events published.

A crash between 2 and 3 republishes the events later. That duplicate delivery is
expected: the worker's atomic claim turns the second message into a no-op.
A crash before 2 leaves the event unpublished and the job QUEUED, and step 1
re-publishes QUEUED jobs, so nothing is stranded.
"""
import logging
from dataclasses import dataclass

import redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from core.logging import sanitize
from database import utcnow
from jobs.models import Job, OutboxEvent
from jobs.state import DISPATCHABLE_STATES, JobStatus
from ops.metrics import OUTBOX_PUBLISHED
from taskqueue import queues

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Pending:
    event_id: object
    job_id: object
    priority: int


def _prepare_batch(db: Session, batch_size: int) -> tuple[list[_Pending], list[object]]:
    """Returns (events to publish, events to retire without publishing)."""
    now = utcnow()
    events = list(db.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now)
        .order_by(OutboxEvent.available_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ))
    to_publish: list[_Pending] = []
    to_skip: list[object] = []
    for event in events:
        job = db.get(Job, event.job_id)
        if job is None:
            to_skip.append(event.id)
            continue
        status = JobStatus(job.status)
        if status in DISPATCHABLE_STATES:
            moved = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == status)
                .values(status=JobStatus.QUEUED, queued_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if moved != 1:
                continue  # changed concurrently (e.g. cancelled); revisit next round
            to_publish.append(_Pending(event.id, job.id, job.priority))
        elif status == JobStatus.QUEUED:
            to_publish.append(_Pending(event.id, job.id, job.priority))
        else:
            # Cancelled, running or finished: the dispatch intent is obsolete.
            to_skip.append(event.id)
    if to_skip:
        db.execute(
            update(OutboxEvent).where(OutboxEvent.id.in_(to_skip))
            .values(published_at=now)
            .execution_options(synchronize_session=False)
        )
    db.commit()
    return to_publish, to_skip


def dispatch_once(session_factory: sessionmaker, client: redis.Redis, batch_size: int) -> int:
    """Publish one batch of due outbox events. Returns the number published."""
    with session_factory() as db:
        to_publish, _ = _prepare_batch(db, batch_size)
    if not to_publish:
        return 0

    event_ids = [p.event_id for p in to_publish]
    try:
        pipe = client.pipeline(transaction=False)
        for pending in to_publish:
            pipe.lpush(queues.queue_for_priority(pending.priority), str(pending.job_id))
        pipe.execute()
    except redis.RedisError as exc:
        logger.error("Outbox publish failed: %s", sanitize(str(exc)))
        with session_factory() as db:
            db.execute(
                update(OutboxEvent).where(OutboxEvent.id.in_(event_ids))
                .values(publish_attempts=OutboxEvent.publish_attempts + 1,
                        last_error=sanitize(str(exc), 500))
                .execution_options(synchronize_session=False)
            )
            db.commit()
        return 0

    with session_factory() as db:
        db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(event_ids), OutboxEvent.published_at.is_(None))
            .values(published_at=utcnow(), publish_attempts=OutboxEvent.publish_attempts + 1)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    OUTBOX_PUBLISHED.inc(len(to_publish))
    return len(to_publish)
