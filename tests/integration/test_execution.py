import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from jobs.models import Job
from taskqueue import queues
from tests.helpers import dispatch, drain, submit

JOBS = "/api/v1/jobs"


def _job(job_id: str) -> Job:
    with SessionLocal() as db:
        return db.get(Job, uuid.UUID(job_id))


def test_end_to_end_success(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], "generate_report", {"report_id": "report-123", "rows": 1500})["job_id"]

    assert dispatch(redis_client) == 1
    assert _job(job_id).status == "QUEUED"
    assert worker.process_one()

    detail = client.get(f"{JOBS}/{job_id}", headers=user["headers"]).json()
    assert detail["status"] == "SUCCEEDED"
    assert detail["attempt_count"] == 1
    assert detail["started_at"] and detail["completed_at"]

    result = client.get(f"{JOBS}/{job_id}/result", headers=user["headers"]).json()
    assert result["result"]["file_name"] == "report-123.csv"
    assert result["result"]["records_processed"] == 1500

    attempts = client.get(f"{JOBS}/{job_id}/attempts", headers=user["headers"]).json()
    assert [(a["attempt_number"], a["status"], a["worker_id"]) for a in attempts] == [(1, "SUCCEEDED", "test-worker")]
    assert attempts[0]["duration_ms"] is not None

    # The message was acknowledged only after the outcome was stored.
    assert redis_client.llen(queues.PROCESSING_KEY.format(worker_id="test-worker")) == 0


def test_all_builtin_handlers_execute(client, user, worker, redis_client):
    ids = [
        submit(client, user["headers"], "process_file",
               {"file_name": "a.txt", "content": "one two\nthree", "operations": ["line_count", "word_count", "top_words"]}
               )["job_id"],
        submit(client, user["headers"], "send_notification",
               {"notification_id": "n-1", "channel": "email", "recipient": "a@example.com", "message": "hi"})["job_id"],
        submit(client, user["headers"], "generate_report", {"report_id": "r", "format": "json", "rows": 3})["job_id"],
    ]
    drain(worker, redis_client)
    jobs = [_job(i) for i in ids]
    assert [j.status for j in jobs] == ["SUCCEEDED"] * 3
    assert jobs[0].result["line_count"] == 2 and jobs[0].result["word_count"] == 3


def test_notification_handler_is_idempotent(client, user, worker, redis_client):
    payload = {"notification_id": "same-id", "channel": "sms", "recipient": "5550100", "message": "hi"}
    first = submit(client, user["headers"], "send_notification", payload)["job_id"]
    second = submit(client, user["headers"], "send_notification", payload)["job_id"]
    drain(worker, redis_client)
    assert _job(first).result["deduplicated"] is False
    assert _job(second).result["deduplicated"] is True


def test_transient_failure_is_retried_then_succeeds(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 1, "duration_ms": 0},
                    max_attempts=3)["job_id"]
    dispatch(redis_client)
    worker.process_one()

    job = _job(job_id)
    assert job.status == "RETRY_WAIT"
    assert job.last_error["retryable"] is True

    drain(worker, redis_client)
    detail = client.get(f"{JOBS}/{job_id}", headers=user["headers"]).json()
    assert detail["status"] == "SUCCEEDED"
    attempts = client.get(f"{JOBS}/{job_id}/attempts", headers=user["headers"]).json()
    assert [a["status"] for a in attempts] == ["FAILED", "SUCCEEDED"]


def test_retry_waits_for_backoff(client, user, worker, redis_client, settings):
    settings.retry_base_delay_seconds = 60
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 1, "duration_ms": 0})["job_id"]
    dispatch(redis_client)
    worker.process_one()

    job = _job(job_id)
    assert job.status == "RETRY_WAIT"
    assert job.scheduled_at > datetime.now(timezone.utc) + timedelta(seconds=50)
    assert dispatch(redis_client) == 0  # not due yet
    assert _job(job_id).status == "RETRY_WAIT"


def test_exhausted_retries_go_to_dead_letter(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 10, "duration_ms": 0},
                    max_attempts=3)["job_id"]
    drain(worker, redis_client)
    job = _job(job_id)
    assert job.status == "DEAD_LETTER"
    assert job.attempt_count == 3
    attempts = client.get(f"{JOBS}/{job_id}/attempts", headers=user["headers"]).json()
    assert len(attempts) == 3 and all(a["error_type"] == "RetryableError" for a in attempts)


def test_permanent_failure_is_not_retried(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 5, "failure": "permanent"},
                    max_attempts=5)["job_id"]
    drain(worker, redis_client)
    job = _job(job_id)
    assert job.status == "FAILED"
    assert job.attempt_count == 1
    assert job.last_error["retryable"] is False


def test_manual_retry_of_dead_letter_job(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], payload={"fail_until_attempt": 1}, max_attempts=1)["job_id"]
    drain(worker, redis_client)
    assert _job(job_id).status == "DEAD_LETTER"

    response = client.post(f"{JOBS}/{job_id}/retry", headers=user["headers"])
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
    assert response.json()["max_attempts"] == 2

    drain(worker, redis_client)
    assert _job(job_id).status == "SUCCEEDED"
    assert _job(job_id).attempt_count == 2


def test_retry_rejected_for_non_terminal_job(client, user):
    job_id = submit(client, user["headers"])["job_id"]
    assert client.post(f"{JOBS}/{job_id}/retry", headers=user["headers"]).status_code == 409


def test_scheduled_job_not_dispatched_early(client, user, redis_client):
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    job_id = submit(client, user["headers"], scheduled_at=later)["job_id"]
    assert dispatch(redis_client) == 0
    assert _job(job_id).status == "PENDING"


def test_priority_routes_to_queue(client, user, redis_client):
    submit(client, user["headers"], priority=9)
    submit(client, user["headers"], priority=5)
    submit(client, user["headers"], priority=1)
    dispatch(redis_client)
    assert queues.queue_depths(redis_client) == {"high": 1, "normal": 1, "low": 1}


def test_cancel_pending_job(client, user, worker, redis_client):
    job_id = submit(client, user["headers"])["job_id"]
    response = client.post(f"{JOBS}/{job_id}/cancel", headers=user["headers"])
    assert response.json()["status"] == "CANCELLED"
    dispatch(redis_client)
    assert queues.queue_depths(redis_client) == {"high": 0, "normal": 0, "low": 0}
    assert client.post(f"{JOBS}/{job_id}/cancel", headers=user["headers"]).status_code == 409


def test_cancel_queued_job_skipped_by_worker(client, user, worker, redis_client):
    job_id = submit(client, user["headers"])["job_id"]
    dispatch(redis_client)
    client.post(f"{JOBS}/{job_id}/cancel", headers=user["headers"])
    assert worker.process_one()  # consumes the message without executing
    job = _job(job_id)
    assert job.status == "CANCELLED" and job.attempt_count == 0


def test_cancel_running_job_cooperatively(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], "test_sleep", {"seconds": 0.4})["job_id"]
    dispatch(redis_client)
    runner = threading.Thread(target=worker.process_one)
    runner.start()
    for _ in range(100):
        if _job(job_id).status == "RUNNING":
            break
        time.sleep(0.01)
    response = client.post(f"{JOBS}/{job_id}/cancel", headers=user["headers"])
    assert response.json()["cancel_requested"] is True
    runner.join(5)

    assert _job(job_id).status == "CANCELLED"
    attempts = client.get(f"{JOBS}/{job_id}/attempts", headers=user["headers"]).json()
    assert attempts[0]["status"] == "CANCELLED"


def test_execution_timeout_is_enforced(client, user, worker, redis_client):
    # test_sleep has a 0.5s timeout; a cooperative handler stops when told.
    job_id = submit(client, user["headers"], "test_sleep", {"seconds": 5}, max_attempts=1)["job_id"]
    dispatch(redis_client)
    started = time.monotonic()
    worker.process_one()
    assert time.monotonic() - started < 3
    job = _job(job_id)
    assert job.status == "DEAD_LETTER"
    assert job.last_error["error_type"] == "Timeout"


def test_timeout_abandons_uncooperative_handler(client, user, worker, redis_client):
    job_id = submit(client, user["headers"], "test_sleep", {"seconds": 4, "cooperative": False},
                    max_attempts=2)["job_id"]
    dispatch(redis_client)
    started = time.monotonic()
    worker.process_one()
    assert time.monotonic() - started < 3.5
    assert _job(job_id).status == "RETRY_WAIT"
    attempts = client.get(f"{JOBS}/{job_id}/attempts", headers=user["headers"]).json()
    assert attempts[0]["status"] == "TIMED_OUT"
