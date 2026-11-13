import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from core.rate_limit import rate_limit
from core.security import get_current_user
from database import get_db
from jobs.schemas.job import (
    JobAttemptResponse,
    JobCreatedResponse,
    JobCreateRequest,
    JobDetail,
    JobListResponse,
    JobResultResponse,
    JobSummary,
)
from jobs.service import job_service
from jobs.service.errors import IdempotencyConflict, InvalidJobRequest, JobNotFound, JobStateConflict
from jobs.state import JobStatus
from users.models.auth import User
from workers.registry import job_type_schemas, registered_job_types

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["Jobs"]
)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, JobNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, InvalidJobRequest):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, (IdempotencyConflict, JobStateConflict)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc


@router.get("/types")
def list_job_types(user: User = Depends(get_current_user)) -> list[str]:
    return registered_job_types()


@router.get("/types/schemas")
def list_job_type_schemas(user: User = Depends(get_current_user)) -> list[dict]:
    return job_type_schemas()


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("submit", lambda s: s.rate_limit_submit_per_minute))],
)
def submit_job(
    request: JobCreateRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=255),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobCreatedResponse:
    try:
        job, created = job_service.submit_job(db, user.id, request, idempotency_key)
    except (InvalidJobRequest, IdempotencyConflict) as exc:
        raise _http_error(exc)
    if not created:
        response.status_code = status.HTTP_200_OK
    return JobCreatedResponse(
        job_id=job.id, status=job.status, created_at=job.created_at, scheduled_at=job.scheduled_at
    )


@router.get("")
def list_jobs(
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    job_type: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobListResponse:
    try:
        jobs, next_cursor = job_service.list_jobs(db, user.id, status_filter, job_type, limit, cursor)
    except InvalidJobRequest as exc:
        raise _http_error(exc)
    return JobListResponse(items=[JobSummary.from_job(job) for job in jobs], next_cursor=next_cursor)


@router.get("/{job_id}")
def get_job(job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> JobDetail:
    try:
        return JobDetail.from_job(job_service.get_job(db, user.id, job_id))
    except JobNotFound as exc:
        raise _http_error(exc)


@router.get("/{job_id}/attempts")
def get_attempts(
    job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[JobAttemptResponse]:
    try:
        return [JobAttemptResponse.from_attempt(a) for a in job_service.list_attempts(db, user.id, job_id)]
    except JobNotFound as exc:
        raise _http_error(exc)


@router.get("/{job_id}/result")
def get_result(
    job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> JobResultResponse:
    try:
        job = job_service.get_job(db, user.id, job_id)
    except JobNotFound as exc:
        raise _http_error(exc)
    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job has no result; current status is {job.status}",
        )
    return JobResultResponse(job_id=job.id, status=job.status, result=job.result)


@router.post(
    "/{job_id}/retry",
    dependencies=[Depends(rate_limit("retry", lambda s: s.rate_limit_retry_per_minute))],
)
def retry_job(job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> JobDetail:
    try:
        return JobDetail.from_job(job_service.retry_job(db, user.id, job_id, actor_id=user.id))
    except (JobNotFound, JobStateConflict) as exc:
        raise _http_error(exc)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> JobDetail:
    try:
        return JobDetail.from_job(job_service.cancel_job(db, user.id, job_id, actor_id=user.id))
    except (JobNotFound, JobStateConflict) as exc:
        raise _http_error(exc)
