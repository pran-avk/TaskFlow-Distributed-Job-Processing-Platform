"""Failure scenarios from README section 16. Each guards a specific reliability property."""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from database import SessionLocal, utcnow
from jobs.models import AuditLog, Job, JobAttempt, OutboxEvent
from jobs.schemas.job import JobCreateRequest
from jobs.service import execution_service, job_service, recovery_service
from jobs.service.errors import JobStateConflict
from taskqueue import queues
from tests.helpers import dispatch, drain, submit
from workers.runner import Worker


def _job(job_id) -> Job:
    with SessionLocal() as db:
        return db.get(Job, uuid.UUID(str(job_id)))


def _attempts(job_id) -> list[JobAttempt]:
    with SessionLocal() as db:
        return list(db.scalars(select(JobAttempt).where(JobAttempt.job_id == uuid.UUID(str(job_id)))
                               .order_by(JobAttempt.attempt_number)))


def _count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_duplicate_queue_messages_execute_once(client, user, worker, redis_client):
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)
    queues.publish(redis_client, job_id, 5)  # duplicate delivery
    queues.publish(redis_client, job_id, 9)

    while worker.process_one():
        pass
    assert _job(job_id).status == "SUCCEEDED"
    assert len(_attempts(job_id)) == 1
    assert redis_client.llen(queues.PROCESSING_KEY.format(worker_id="test-worker")) == 0


def test_repeated_dispatch_events_are_harmless(client, user, worker, redis_client):
    """Dispatcher crashed after pushing to Redis but before marking the event published."""
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)
    with SessionLocal() as db:
        db.execute(update(OutboxEvent).values(published_at=None))
        db.commit()
    assert dispatch(redis_client) == 1  # republished
    assert queues.queue_depths(redis_client)["normal"] == 2

    while worker.process_one():
        pass
    assert _job(job_id).status == "SUCCEEDED"
    assert len(_attempts(job_id)) == 1


def test_redis_unavailable_during_dispatch(client, user, worker, redis_client, monkeypatch):
    job_id = submit(client, user["headers"])["job_id"]

    class DownPipeline:
        def lpush(self, *args):
            pass

        def execute(self):
            raise redis.ConnectionError("Redis is down")

    monkeypatch.setattr(redis_client, "pipeline", lambda **kwargs: DownPipeline())
    assert dispatch(redis_client) == 0
    monkeypatch.undo()

    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent))
        assert event.published_at is None
        assert event.publish_attempts == 1
        assert "Redis is down" in event.last_error

    # Redis is back: the same event is published and the job runs.
    assert dispatch(redis_client) == 1
    worker.process_one()
    assert _job(job_id).status == "SUCCEEDED"


def test_redis_unavailable_during_submission_does_not_lose_jobs(client, user, worker, redis_client, monkeypatch):
    """Submission only touches PostgreSQL (rate limiter fails open), so jobs are accepted during a Redis outage."""
    def down(*args, **kwargs):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(redis_client, "pipeline", down)
    job_id = submit(client, user["headers"])["job_id"]
    monkeypatch.undo()
    drain(worker, redis_client)
    assert _job(job_id).status == "SUCCEEDED"


def test_postgres_unavailable_during_claim_requeues_message(client, user, redis_client, monkeypatch):
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)

    def broken_session():
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    broken_worker = Worker(session_factory=broken_session, redis_client=redis_client, worker_id="w-broken")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    broken_worker.process_one()

    assert redis_client.llen(queues.PROCESSING_KEY.format(worker_id="w-broken")) == 0
    assert redis_client.lrange(queues.QUEUE_HIGH, 0, -1) == [job_id]
    assert _job(job_id).status == "QUEUED"


def test_expired_lease_is_recovered_and_stale_worker_is_fenced(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], max_attempts=3)["job_id"]
    dispatch(redis_client)
    with SessionLocal() as db:
        stale_claim = execution_service.claim_job(db, uuid.UUID(job_id), "crashed-worker", lease_seconds=30)
    with SessionLocal() as db:
        db.execute(update(Job).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()

    assert recovery_service.recover_expired_leases(SessionLocal) == 1
    job = _job(job_id)
    assert job.status == "RETRY_WAIT"
    assert job.last_error["error_type"] == "LeaseExpired"
    assert _attempts(job_id)[0].status == "LOST"

    # The "crashed" worker wakes up and tries to record success: rejected by the fence.
    with SessionLocal() as db:
        assert execution_service.complete_job(db, stale_claim, {"stale": True}, 10) is False

    redis_client.delete(queues.QUEUE_NORMAL)  # drop the original message
    drain(worker, redis_client)
    job = _job(job_id)
    assert job.status == "SUCCEEDED"
    assert job.result != {"stale": True}
    assert [a.status for a in _attempts(job_id)] == ["LOST", "SUCCEEDED"]


def test_stale_worker_cannot_overwrite_newer_attempt(client, user, redis_client):
    job_id = uuid.UUID(submit(client, user["headers"])["job_id"])
    dispatch(redis_client)
    with SessionLocal() as db:
        first = execution_service.claim_job(db, job_id, "w1", 30)
    with SessionLocal() as db:
        db.execute(update(Job).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    recovery_service.recover_expired_leases(SessionLocal)
    with SessionLocal() as db:
        db.execute(update(Job).values(status="QUEUED"))
        db.commit()
    with SessionLocal() as db:
        second = execution_service.claim_job(db, job_id, "w2", 30)
    assert second.attempt_number == 2

    with SessionLocal() as db:
        assert execution_service.fail_job(db, first, "Boom", "late failure", True, 5) is None
    with SessionLocal() as db:
        assert execution_service.renew_lease(db, first, 30) == (False, False)
    with SessionLocal() as db:
        assert execution_service.complete_job(db, second, {"ok": True}, 5) is True
    assert _job(job_id).result == {"ok": True}


def test_live_lease_is_not_recovered(client, user, redis_client):
    job_id = uuid.UUID(submit(client, user["headers"])["job_id"])
    dispatch(redis_client)
    with SessionLocal() as db:
        execution_service.claim_job(db, job_id, "w1", 30)
    assert recovery_service.recover_expired_leases(SessionLocal) == 0
    assert _job(job_id).status == "RUNNING"


def test_lease_expiry_with_no_attempts_left_dead_letters(client, user, redis_client):
    job_id = uuid.UUID(submit(client, user["headers"], max_attempts=1)["job_id"])
    dispatch(redis_client)
    with SessionLocal() as db:
        execution_service.claim_job(db, job_id, "w1", 30)
        db.execute(update(Job).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    recovery_service.recover_expired_leases(SessionLocal)
    assert _job(job_id).status == "DEAD_LETTER"


def test_worker_crash_messages_are_requeued(client, user, worker, redis_client):
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)
    # A worker took the message, then died before claiming (its heartbeat key has expired).
    assert queues.fetch(redis_client, "dead-worker", (6, 3, 1)) == job_id
    redis_client.sadd(queues.WORKERS_SET, "dead-worker")

    assert recovery_service.requeue_orphaned_messages(redis_client) == 1
    assert not redis_client.sismember(queues.WORKERS_SET, "dead-worker")
    worker.process_one()
    assert _job(job_id).status == "SUCCEEDED"


def test_live_worker_messages_are_left_alone(redis_client, worker):
    worker.heartbeat()
    redis_client.lpush(queues.PROCESSING_KEY.format(worker_id="test-worker"), "in-flight")
    assert recovery_service.requeue_orphaned_messages(redis_client) == 0


def test_lost_redis_message_is_redispatched(client, user, worker, redis_client):
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)
    redis_client.flushdb()  # Redis restarted without persistence

    assert recovery_service.redispatch_stale_queued(SessionLocal, stale_after_seconds=3600) == 0
    with SessionLocal() as db:
        db.execute(update(Job).values(queued_at=utcnow() - timedelta(hours=2)))
        db.commit()
    assert recovery_service.redispatch_stale_queued(SessionLocal, stale_after_seconds=3600) == 1
    # No second recovery event while the first is still pending.
    assert recovery_service.redispatch_stale_queued(SessionLocal, stale_after_seconds=0) == 0

    drain(worker, redis_client)
    assert _job(job_id).status == "SUCCEEDED"


def test_concurrent_claims_have_one_winner(client, user, redis_client):
    job_id = uuid.UUID(submit(client, user["headers"])["job_id"])
    dispatch(redis_client)

    def claim(i):
        with SessionLocal() as db:
            return execution_service.claim_job(db, job_id, f"w{i}", 30)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert sum(r is not None for r in results) == 1
    assert len(_attempts(job_id)) == 1


def test_concurrent_retry_requests_dispatch_once(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 1}, max_attempts=1)["job_id"]
    drain(worker, redis_client)
    assert _job(job_id).status == "DEAD_LETTER"
    events_before = _count(OutboxEvent)

    def retry(_):
        with SessionLocal() as db:
            try:
                job_service.retry_job(db, user["user_id"], uuid.UUID(job_id), user["user_id"])
                return "ok"
            except JobStateConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(retry, range(8)))
    assert outcomes.count("ok") == 1
    assert _count(OutboxEvent) == events_before + 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "job.retry")) == 1


def test_concurrent_duplicate_submissions_create_one_job(client, user):
    request = JobCreateRequest(job_type="generate_report", payload={"report_id": "r1"})

    def submit_once(_):
        with SessionLocal() as db:
            job, created = job_service.submit_job(db, user["user_id"], request, "same-key")
            return job.id, created

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit_once, range(8)))
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert _count(Job) == 1
    assert _count(OutboxEvent) == 1


def test_graceful_shutdown_finishes_in_flight_work(client, user, redis_client, settings):
    settings.worker_shutdown_grace_seconds = 5
    job_id = submit(client, user["headers"], "test_sleep", {"seconds": 0.3})["job_id"]
    dispatch(redis_client)
    worker = Worker(session_factory=SessionLocal, redis_client=redis_client, worker_id="graceful")
    thread = threading.Thread(target=worker.run)
    thread.start()
    for _ in range(200):
        if _job(job_id).status == "RUNNING":
            break
        time.sleep(0.01)
    worker.stop()
    thread.join(10)

    assert not thread.is_alive()
    assert _job(job_id).status == "SUCCEEDED"
    assert not redis_client.exists(queues.WORKER_KEY.format(worker_id="graceful"))
    assert not redis_client.sismember(queues.WORKERS_SET, "graceful")


def test_shutdown_after_grace_interrupts_and_leaves_job_retryable(client, user, redis_client, settings):
    settings.worker_shutdown_grace_seconds = 0.1
    job_id = submit(client, user["headers"], "simulated_work", {"duration_ms": 20_000}, max_attempts=3)["job_id"]
    dispatch(redis_client)
    worker = Worker(session_factory=SessionLocal, redis_client=redis_client, worker_id="impatient")
    thread = threading.Thread(target=worker.run)
    thread.start()
    for _ in range(200):
        if _job(job_id).status == "RUNNING":
            break
        time.sleep(0.01)
    worker.stop()
    thread.join(10)

    job = _job(job_id)
    assert job.status == "RETRY_WAIT"
    assert job.last_error["error_type"] == "WorkerShutdown"
