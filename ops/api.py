import time

import redis
from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from core.config import get_settings
from core.security import get_current_user, require_metrics_token
from database import get_db
from jobs.models import Job, OutboxEvent
from taskqueue import queues
from taskqueue.redis_client import get_redis

router = APIRouter(prefix="/api/v1", tags=["Operations"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(response: Response, db: Session = Depends(get_db)) -> dict:
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except redis.RedisError:
        checks["redis"] = "unavailable"
    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "unavailable", "checks": checks}


@router.get("/queues/stats", dependencies=[Depends(get_current_user)])
def queue_stats(db: Session = Depends(get_db)) -> dict:
    client = get_redis()
    processing = sum(
        client.llen(queues.PROCESSING_KEY.format(worker_id=w)) for w in client.smembers(queues.WORKERS_SET)
    )
    by_status = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    oldest_queued = db.scalar(select(func.min(Job.queued_at)).where(Job.status == "QUEUED"))
    outbox_pending = db.scalar(
        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
    )
    return {
        "queues": queues.queue_depths(client),
        "in_flight_messages": processing,
        "outbox_pending": outbox_pending,
        "jobs_by_status": by_status,
        "oldest_queued_at": oldest_queued,
    }


@router.get("/workers", dependencies=[Depends(get_current_user)])
def workers() -> list[dict]:
    client = get_redis()
    stale_after = get_settings().worker_heartbeat_interval_seconds * 3
    now = time.time()
    result = []
    for worker_id in sorted(client.smembers(queues.WORKERS_SET)):
        info = client.hgetall(queues.WORKER_KEY.format(worker_id=worker_id))
        if not info:
            result.append({"worker_id": worker_id, "healthy": False, "state": "lost"})
            continue
        age = now - float(info.get("last_heartbeat", 0))
        result.append({
            "worker_id": worker_id,
            "hostname": info.get("hostname"),
            "state": info.get("state"),
            "healthy": age <= stale_after,
            "heartbeat_age_seconds": round(age, 2),
            "active_jobs": int(info.get("active_jobs", 0)),
            "concurrency": int(info.get("concurrency", 0)),
            "processed": int(info.get("processed", 0)),
            "failed": int(info.get("failed", 0)),
            "uptime_seconds": round(now - float(info.get("started_at", now)), 1),
        })
    return result


@router.get("/metrics", dependencies=[Depends(require_metrics_token)], include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
