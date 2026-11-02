import base64
import hashlib
import json
import uuid
from datetime import datetime

import pydantic
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import utcnow
from jobs.models import AuditLog, Job, JobAttempt, OutboxEvent
from jobs.schemas.job import JobCreateRequest
from jobs.service.errors import IdempotencyConflict, InvalidJobRequest, JobNotFound, JobStateConflict
from jobs.state import (
    DIRECTLY_CANCELLABLE_STATES,
    MANUALLY_RETRYABLE_STATES,
    TERMINAL_STATES,
    JobStatus,
)
from ops.metrics import JOBS_SUBMITTED
from workers.errors import UnknownJobTypeError
from workers.registry import validate_payload


def request_hash(request: JobCreateRequest, normalized_payload: dict) -> str:
    body = {
        "job_type": request.job_type,
        "payload": normalized_payload,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "scheduled_at": request.scheduled_at.isoformat() if request.scheduled_at else None,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _find_by_idempotency_key(db: Session, tenant_id: int, key: str) -> Job | None:
    return db.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == key))


def _replay_or_conflict(existing: Job, hash_value: str) -> Job:
    if existing.request_hash != hash_value:
        raise IdempotencyConflict("Idempotency-Key was already used with a different request")
    return existing


def submit_job(
    db: Session,
    tenant_id: int,
    request: JobCreateRequest,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    """Persist a job and its dispatch event atomically. Returns (job, created)."""
    try:
        payload = validate_payload(request.job_type, request.payload)
    except UnknownJobTypeError as exc:
        raise InvalidJobRequest(str(exc)) from exc
    except pydantic.ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidJobRequest(f"Invalid payload for {request.job_type}: {details}") from exc

    hash_value = request_hash(request, payload)
    if idempotency_key:
        existing = _find_by_idempotency_key(db, tenant_id, idempotency_key)
        if existing is not None:
            return _replay_or_conflict(existing, hash_value), False

    now = utcnow()
    scheduled_at = request.scheduled_at or now
    job = Job(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        job_type=request.job_type,
        payload=payload,
        status=JobStatus.PENDING,
        priority=request.priority,
        max_attempts=request.max_attempts,
        attempt_count=0,
        scheduled_at=scheduled_at,
        created_at=now,
        updated_at=now,
        idempotency_key=idempotency_key,
        request_hash=hash_value,
    )
    db.add(job)
    db.add(OutboxEvent(job_id=job.id, available_at=scheduled_at, payload={"reason": "submitted"}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent request with the same key won the race.
        if idempotency_key:
            existing = _find_by_idempotency_key(db, tenant_id, idempotency_key)
            if existing is not None:
                return _replay_or_conflict(existing, hash_value), False
        raise
    JOBS_SUBMITTED.labels(job_type=job.job_type).inc()
    return job, True


def get_job(db: Session, tenant_id: int, job_id: uuid.UUID) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id))
    if job is None:
        raise JobNotFound("Job not found")
    return job


def _encode_cursor(job: Job) -> str:
    raw = f"{job.created_at.isoformat()}|{job.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_raw, id_raw = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created_raw), uuid.UUID(id_raw)
    except Exception as exc:
        raise InvalidJobRequest("Invalid cursor") from exc


def list_jobs(
    db: Session,
    tenant_id: int,
    status: JobStatus | None = None,
    job_type: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[Job], str | None]:
    query = select(Job).where(Job.tenant_id == tenant_id)
    if status is not None:
        query = query.where(Job.status == status)
    if job_type is not None:
        query = query.where(Job.job_type == job_type)
    if cursor:
        created_at, last_id = _decode_cursor(cursor)
        query = query.where(
            or_(Job.created_at < created_at, and_(Job.created_at == created_at, Job.id < last_id))
        )
    rows = list(db.scalars(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)))
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


def list_attempts(db: Session, tenant_id: int, job_id: uuid.UUID) -> list[JobAttempt]:
    get_job(db, tenant_id, job_id)
    return list(db.scalars(
        select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_number)
    ))


def cancel_job(db: Session, tenant_id: int, job_id: uuid.UUID, actor_id: int) -> Job:
    """Cancel immediately when not running; otherwise request cooperative cancellation."""
    for _ in range(3):
        job = get_job(db, tenant_id, job_id)
        current = JobStatus(job.status)
        now = utcnow()
        if current in TERMINAL_STATES:
            raise JobStateConflict(f"Job is already {current}")

        if current in DIRECTLY_CANCELLABLE_STATES:
            values = dict(status=JobStatus.CANCELLED, completed_at=now, updated_at=now, cancel_requested=True)
        else:  # RUNNING
            values = dict(cancel_requested=True, updated_at=now)

        updated = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == current)
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
        if updated == 1:
            db.add(AuditLog(actor_user_id=actor_id, action="job.cancel", job_id=job_id,
                            details={"from_status": str(current)}))
            db.commit()
            db.refresh(job)
            return job
        # The job changed state concurrently; re-evaluate.
        db.rollback()
    raise JobStateConflict("Job state changed concurrently, try again")


def retry_job(db: Session, tenant_id: int, job_id: uuid.UUID, actor_id: int) -> Job:
    """Manually re-run a terminal job. The conditional UPDATE guarantees that concurrent
    retry requests produce exactly one new dispatch."""
    job = get_job(db, tenant_id, job_id)
    now = utcnow()
    # Grant at least one more attempt beyond those already used.
    greatest = func.max if db.get_bind().dialect.name == "sqlite" else func.greatest
    updated = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_([str(s) for s in MANUALLY_RETRYABLE_STATES]))
        .values(
            status=JobStatus.PENDING,
            max_attempts=greatest(Job.max_attempts, Job.attempt_count + 1),
            scheduled_at=now,
            completed_at=None,
            cancel_requested=False,
            result=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        db.rollback()
        db.refresh(job)
        raise JobStateConflict(f"Job in status {job.status} cannot be retried")

    db.add(OutboxEvent(job_id=job_id, available_at=now, payload={"reason": "manual_retry"}))
    db.add(AuditLog(actor_user_id=actor_id, action="job.retry", job_id=job_id,
                    details={"from_status": job.status, "attempt_count": job.attempt_count}))
    db.commit()
    db.refresh(job)
    return job
