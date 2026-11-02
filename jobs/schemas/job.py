import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.config import get_settings
from jobs.models import Job, JobAttempt


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)
    priority: int = Field(default=5, ge=0, le=9, description="0 (lowest) to 9 (highest)")
    max_attempts: int = Field(default=3, ge=1, le=20)
    scheduled_at: datetime | None = Field(default=None, description="Earliest execution time (UTC if no offset)")

    @field_validator("payload")
    @classmethod
    def payload_size_limit(cls, value: dict) -> dict:
        limit = get_settings().max_payload_bytes
        if len(json.dumps(value)) > limit:
            raise ValueError(f"payload exceeds {limit} bytes")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    created_at: datetime
    scheduled_at: datetime


class JobSummary(BaseModel):
    job_id: uuid.UUID
    job_type: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    scheduled_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_job(cls, job: Job) -> "JobSummary":
        return cls(
            job_id=job.id,
            job_type=job.job_type,
            status=job.status,
            priority=job.priority,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            created_at=job.created_at,
            scheduled_at=job.scheduled_at,
            completed_at=job.completed_at,
        )


class JobDetail(JobSummary):
    payload: dict
    updated_at: datetime
    started_at: datetime | None
    cancel_requested: bool
    idempotency_key: str | None
    result: dict | None
    last_error: dict | None

    @classmethod
    def from_job(cls, job: Job) -> "JobDetail":
        return cls(
            **JobSummary.from_job(job).model_dump(),
            payload=job.payload,
            updated_at=job.updated_at,
            started_at=job.started_at,
            cancel_requested=job.cancel_requested,
            idempotency_key=job.idempotency_key,
            result=job.result,
            last_error=job.last_error,
        )


class JobListResponse(BaseModel):
    items: list[JobSummary]
    next_cursor: str | None


class JobAttemptResponse(BaseModel):
    attempt_number: int
    worker_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_type: str | None
    error_message: str | None
    duration_ms: int | None

    @classmethod
    def from_attempt(cls, attempt: JobAttempt) -> "JobAttemptResponse":
        return cls(
            attempt_number=attempt.attempt_number,
            worker_id=attempt.worker_id,
            status=attempt.status,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            error_type=attempt.error_type,
            error_message=attempt.error_message,
            duration_ms=attempt.duration_ms,
        )


class JobResultResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    result: dict | None
