import logging
from datetime import timedelta

import redis
from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import sessionmaker

from database import utcnow
from jobs.models import Job, OutboxEvent
from jobs.service.execution_service import ClaimedJob, fail_job
from jobs.state import AttemptStatus, JobStatus
from ops.metrics import JOB_RETRIES, JOBS_COMPLETED, RECOVERED
from taskqueue import queues

logger = logging.getLogger(__name__)


def recover_expired_leases(session_factory: sessionmaker, limit: int = 100) -> int:
    """RUNNING jobs whose worker stopped renewing its lease are failed as a retryable
    attempt. The fenced update also requires the lease to still be expired, so a
    worker that renews at the last moment keeps its job."""
    now = utcnow()
    with session_factory() as db:
        expired = list(db.scalars(
            select(Job)
            .where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now)
            .limit(limit)
        ))
        claims = [
            ClaimedJob(
                job_id=job.id, tenant_id=job.tenant_id, job_type=job.job_type, payload=job.payload,
                priority=job.priority, attempt_number=job.attempt_count, worker_id=job.worker_id or "",
                scheduled_at=job.scheduled_at, started_at=job.started_at or now,
            )
            for job in expired
        ]
        db.rollback()

    recovered = 0
    for claim in claims:
        with session_factory() as db:
            new_status = fail_job(
                db, claim,
                error_type="LeaseExpired",
                error_message=f"Worker {claim.worker_id} stopped renewing its lease",
                retryable=True,
                duration_ms=None,
                attempt_status=AttemptStatus.LOST,
                lease_expired_before=now,
            )
        if new_status is not None:
            recovered += 1
            RECOVERED.labels(kind="expired_lease").inc()
            JOBS_COMPLETED.labels(job_type=claim.job_type, outcome="lost").inc()
            if new_status == JobStatus.RETRY_WAIT:
                JOB_RETRIES.labels(job_type=claim.job_type).inc()
            logger.warning("Recovered expired lease", extra={"job_id": claim.job_id, "status": new_status})
    return recovered


def redispatch_stale_queued(session_factory: sessionmaker, stale_after_seconds: int, limit: int = 500) -> int:
    """QUEUED jobs that have not started for a long time may have lost their Redis
    message (Redis restart, eviction). Re-dispatch them; duplicates are harmless."""
    now = utcnow()
    cutoff = now - timedelta(seconds=stale_after_seconds)
    with session_factory() as db:
        has_pending_event = exists().where(
            OutboxEvent.job_id == Job.id, OutboxEvent.published_at.is_(None)
        )
        stale_ids = list(db.scalars(
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED, Job.queued_at < cutoff, ~has_pending_event)
            .limit(limit)
        ))
        for job_id in stale_ids:
            touched = db.execute(
                update(Job).where(Job.id == job_id, Job.status == JobStatus.QUEUED, Job.queued_at < cutoff)
                .values(queued_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if touched:
                db.add(OutboxEvent(job_id=job_id, available_at=now, payload={"reason": "stale_queued"}))
        db.commit()
    if stale_ids:
        RECOVERED.labels(kind="stale_queued").inc(len(stale_ids))
        logger.warning("Re-dispatched %d stale queued jobs", len(stale_ids))
    return len(stale_ids)


def requeue_orphaned_messages(client: redis.Redis) -> int:
    """Return messages held by workers whose heartbeat expired to the queue."""
    moved = 0
    for worker_id in client.smembers(queues.WORKERS_SET):
        if client.exists(queues.WORKER_KEY.format(worker_id=worker_id)):
            continue
        processing = queues.PROCESSING_KEY.format(worker_id=worker_id)
        while client.lmove(processing, queues.QUEUE_HIGH, "RIGHT", "LEFT") is not None:
            moved += 1
        client.srem(queues.WORKERS_SET, worker_id)
        logger.warning("Removed dead worker %s", worker_id, extra={"worker_id": worker_id})
    if moved:
        RECOVERED.labels(kind="orphaned_message").inc(moved)
    return moved


def purge_published_outbox(session_factory: sessionmaker, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    with session_factory() as db:
        removed = db.execute(
            delete(OutboxEvent).where(OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < cutoff)
        ).rowcount
        db.commit()
    return removed
