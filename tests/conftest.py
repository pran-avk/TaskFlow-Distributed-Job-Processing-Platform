"""Shared fixtures.

By default tests use a temporary SQLite database and an in-memory fake Redis.
Set TEST_DATABASE_URL / TEST_REDIS_URL to run the same suite against real
PostgreSQL and Redis (CI does this).
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="taskflow-tests-")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", f"sqlite:///{os.path.join(_tmpdir, 'test.db')}"
)
os.environ.update({
    "JWT_SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256",
    "JWT_ALGORITHM": "HS256",
    "METRICS_TOKEN": "test-metrics-token",
    "LOG_JSON": "false",
    "LOG_LEVEL": "WARNING",
    "RETRY_BASE_DELAY_SECONDS": "0",
    "RETRY_JITTER_SECONDS": "0",
    "WORKER_HEARTBEAT_INTERVAL_SECONDS": "0.1",
    "WORKER_POLL_INTERVAL_SECONDS": "0.05",
    "METRICS_PORT": "0",
    "WORKER_CONCURRENCY": "2",
    "RATE_LIMIT_SUBMIT_PER_MINUTE": "1000",
    "RATE_LIMIT_RETRY_PER_MINUTE": "1000",
})

import fakeredis  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy import delete  # noqa: E402

import main  # noqa: E402
from core.config import get_settings  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from jobs.models import AuditLog, Job, JobAttempt, OutboxEvent, User  # noqa: E402
from taskqueue.redis_client import set_redis  # noqa: E402
from tests.helpers import signup  # noqa: E402
from workers.context import JobContext  # noqa: E402
from workers.registry import job_handler  # noqa: E402
from workers.runner import Worker  # noqa: E402


class SleepPayload(BaseModel):
    seconds: float = Field(default=0.0, ge=0, le=30)
    cooperative: bool = True


@job_handler("test_sleep", SleepPayload, timeout_seconds=0.5)
def _test_sleep(payload: SleepPayload, ctx: JobContext) -> dict:
    """Test-only handler with a short timeout."""
    if payload.cooperative:
        ctx.sleep(payload.seconds)
    else:
        import time
        time.sleep(payload.seconds)
    return {"slept": payload.seconds}


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def redis_client():
    url = os.environ.get("TEST_REDIS_URL")
    client = redis.Redis.from_url(url, decode_responses=True) if url else fakeredis.FakeRedis(decode_responses=True)
    client.flushdb()
    set_redis(client)
    yield client
    client.flushdb()
    set_redis(None)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with SessionLocal() as db:
        for model in (AuditLog, JobAttempt, OutboxEvent, Job, User):
            db.execute(delete(model))
        db.commit()


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for key, value in snapshot.items():
        setattr(s, key, value)


@pytest.fixture
def client(redis_client):
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def user(client):
    return signup(client)


@pytest.fixture
def worker(redis_client):
    return Worker(session_factory=SessionLocal, redis_client=redis_client, worker_id="test-worker")
