"""Worker-side state changes. Every write after the claim is fenced on
(status=RUNNING, attempt_count, worker_id): a worker whose lease was recovered
by the scheduler can no longer overwrite the newer attempt's outcome."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.config import get_settings
from database import utcnow
from jobs.models import Job, JobAttempt, OutboxEvent
from jobs.service.retry_service import compute_backoff, outcome_after_failure
from jobs.state import AttemptStatus, JobStatus


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    tenant_id: int
    job_type: str
    payload: dict
    priority: int
    attempt_number: int
    worker_id: str
    scheduled_at: datetime
    started_at: datetime


def _fenced(claim: ClaimedJob):
    return (
        Job.id == claim.job_id,
        Job.status == JobStatus.RUNNING,
        Job.attempt_count == claim.attempt_number,
        Job.worker_id == claim.worker_id,
    )


def claim_job(db: Session, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    """Atomically move a QUEUED job to RUNNING. Returns None for duplicate or stale
    messages (job missing, already claimed, cancelled, or finished)."""
    job = db.get(Job, job_id)
    if job is None or job.status != JobStatus.QUEUED:
        db.rollback()
        return None

    now = utcnow()
    if job.cancel_requested:
        db.execute(
            update(Job).where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(status=JobStatus.CANCELLED, completed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return None
    if job.attempt_count >= job.max_attempts:
        db.execute(
            update(Job).where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(status=JobStatus.DEAD_LETTER, completed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return None

    attempt_number = job.attempt_count + 1
    claimed = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.QUEUED, Job.attempt_count == job.attempt_count)
        .values(
            status=JobStatus.RUNNING,
            attempt_count=attempt_number,
            worker_id=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        return None

    db.add(JobAttempt(
        job_id=job_id,
        attempt_number=attempt_number,
        worker_id=worker_id,
        status=AttemptStatus.RUNNING,
        started_at=now,
    ))
    db.commit()
    return ClaimedJob(
        job_id=job.id,
        tenant_id=job.tenant_id,
        job_type=job.job_type,
        payload=job.payload,
        priority=job.priority,
        attempt_number=attempt_number,
        worker_id=worker_id,
        scheduled_at=job.scheduled_at,
        started_at=now,
    )


def renew_lease(db: Session, claim: ClaimedJob, lease_seconds: int) -> tuple[bool, bool]:
    """Extend the lease. Returns (still_owner, cancel_requested)."""
    now = utcnow()
    renewed = db.execute(
        update(Job).where(*_fenced(claim))
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        .execution_options(synchronize_session=False)
    ).rowcount
    cancel_requested = bool(db.scalar(select(Job.cancel_requested).where(Job.id == claim.job_id)))
    db.commit()
    return renewed == 1, cancel_requested


def _finish_attempt(db: Session, claim: ClaimedJob, status: AttemptStatus, finished_at: datetime,
                    duration_ms: int | None, error_type: str | None = None,
                    error_message: str | None = None) -> None:
    db.execute(
        update(JobAttempt)
        .where(JobAttempt.job_id == claim.job_id, JobAttempt.attempt_number == claim.attempt_number)
        .values(status=status, finished_at=finished_at, duration_ms=duration_ms,
                error_type=error_type, error_message=error_message)
        .execution_options(synchronize_session=False)
    )


def complete_job(db: Session, claim: ClaimedJob, result: dict, duration_ms: int) -> bool:
    now = utcnow()
    updated = db.execute(
        update(Job).where(*_fenced(claim))
        .values(status=JobStatus.SUCCEEDED, result=result, completed_at=now, updated_at=now,
                lease_expires_at=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        db.rollback()
        return False
    _finish_attempt(db, claim, AttemptStatus.SUCCEEDED, now, duration_ms)
    db.commit()
    return True


def cancel_running_job(db: Session, claim: ClaimedJob, duration_ms: int) -> bool:
    now = utcnow()
    updated = db.execute(
        update(Job).where(*_fenced(claim))
        .values(status=JobStatus.CANCELLED, completed_at=now, updated_at=now, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        db.rollback()
        return False
    _finish_attempt(db, claim, AttemptStatus.CANCELLED, now, duration_ms, "Cancelled", "Cancelled on request")
    db.commit()
    return True


def fail_job(
    db: Session,
    claim: ClaimedJob,
    error_type: str,
    error_message: str,
    retryable: bool,
    duration_ms: int | None,
    attempt_status: AttemptStatus = AttemptStatus.FAILED,
    lease_expired_before: datetime | None = None,
) -> JobStatus | None:
    """Record a failed attempt and decide between RETRY_WAIT, FAILED and DEAD_LETTER.
    Returns the new job status, or None if this caller no longer owns the attempt."""
    settings = get_settings()
    now = utcnow()
    max_attempts = db.scalar(select(Job.max_attempts).where(Job.id == claim.job_id))
    if max_attempts is None:
        db.rollback()
        return None

    new_status = outcome_after_failure(retryable, claim.attempt_number, max_attempts)
    values: dict = dict(
        status=new_status,
        updated_at=now,
        lease_expires_at=None,
        last_error={
            "error_type": error_type,
            "message": error_message,
            "attempt": claim.attempt_number,
            "retryable": retryable,
            "at": now.isoformat(),
        },
    )
    retry_at = None
    if new_status == JobStatus.RETRY_WAIT:
        retry_at = now + timedelta(seconds=compute_backoff(
            claim.attempt_number,
            settings.retry_base_delay_seconds,
            settings.retry_max_delay_seconds,
            settings.retry_jitter_seconds,
        ))
        values["scheduled_at"] = retry_at
    else:
        values["completed_at"] = now

    conditions = list(_fenced(claim))
    if lease_expired_before is not None:
        conditions.append(Job.lease_expires_at < lease_expired_before)
    updated = db.execute(
        update(Job).where(*conditions).values(**values).execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        db.rollback()
        return None

    _finish_attempt(db, claim, attempt_status, now, duration_ms, error_type, error_message)
    if retry_at is not None:
        db.add(OutboxEvent(job_id=claim.job_id, available_at=retry_at,
                           payload={"reason": "retry", "attempt": claim.attempt_number}))
    db.commit()
    return new_status
