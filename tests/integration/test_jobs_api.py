import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from database import SessionLocal
from jobs.models import Job, OutboxEvent
from tests.helpers import submit

JOBS = "/api/v1/jobs"


def test_submit_persists_job_and_outbox_event(client, user):
    body = submit(client, user["headers"], "generate_report", {"report_id": "report-123", "format": "csv"},
                  priority=5, max_attempts=3)
    assert body["status"] == "PENDING"

    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(body["job_id"]))
        assert job.tenant_id == user["user_id"]
        assert job.payload == {"report_id": "report-123", "format": "csv", "rows": 1500}
        events = db.scalars(select(OutboxEvent).where(OutboxEvent.job_id == job.id)).all()
        assert len(events) == 1 and events[0].published_at is None


def test_get_job_details(client, user):
    job_id = submit(client, user["headers"])["job_id"]
    detail = client.get(f"{JOBS}/{job_id}", headers=user["headers"]).json()
    assert detail["job_id"] == job_id
    assert detail["status"] == "PENDING"
    assert detail["attempt_count"] == 0


def test_unknown_job_type_rejected(client, user):
    response = client.post(JOBS, json={"job_type": "run_python", "payload": {"code": "1"}}, headers=user["headers"])
    assert response.status_code == 422
    assert "Unsupported job type" in response.json()["detail"]


def test_invalid_payload_rejected(client, user):
    response = client.post(JOBS, json={"job_type": "generate_report", "payload": {"format": "csv"}},
                           headers=user["headers"])
    assert response.status_code == 422
    assert "report_id" in response.json()["detail"]


def test_request_validation(client, user):
    for body in (
        {"job_type": "simulated_work", "priority": 10},
        {"job_type": "simulated_work", "max_attempts": 0},
        {"job_type": "simulated_work", "unexpected": True},
    ):
        assert client.post(JOBS, json=body, headers=user["headers"]).status_code == 422


def test_payload_size_limit(client, user, settings):
    settings.max_payload_bytes = 100
    response = client.post(JOBS, json={"job_type": "process_file",
                                       "payload": {"file_name": "a", "content": "x" * 500}},
                           headers=user["headers"])
    assert response.status_code == 422


def test_request_body_size_limit(client, user, settings):
    settings.max_request_bytes = 200
    response = client.post(JOBS, json={"job_type": "process_file",
                                       "payload": {"file_name": "a", "content": "x" * 1000}},
                           headers=user["headers"])
    assert response.status_code == 413


def test_idempotent_replay_returns_existing_job(client, user):
    headers = {**user["headers"], "Idempotency-Key": "report-2026-001"}
    body = {"job_type": "generate_report", "payload": {"report_id": "r1"}}
    first = client.post(JOBS, json=body, headers=headers)
    second = client.post(JOBS, json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_idempotency_key_with_different_body_conflicts(client, user):
    headers = {**user["headers"], "Idempotency-Key": "k1"}
    client.post(JOBS, json={"job_type": "generate_report", "payload": {"report_id": "r1"}}, headers=headers)
    conflict = client.post(JOBS, json={"job_type": "generate_report", "payload": {"report_id": "r2"}}, headers=headers)
    assert conflict.status_code == 409


def test_list_jobs_paginates_with_cursor(client, user):
    ids = [submit(client, user["headers"])["job_id"] for _ in range(5)]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get(JOBS, params=params, headers=user["headers"]).json()
        seen += [item["job_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen)) == 5


def test_list_jobs_filters(client, user):
    submit(client, user["headers"], "generate_report", {"report_id": "r"})
    submit(client, user["headers"])
    by_type = client.get(JOBS, params={"job_type": "generate_report"}, headers=user["headers"]).json()
    assert [j["job_type"] for j in by_type["items"]] == ["generate_report"]
    by_status = client.get(JOBS, params={"status": "SUCCEEDED"}, headers=user["headers"]).json()
    assert by_status["items"] == []
    assert client.get(JOBS, params={"status": "BOGUS"}, headers=user["headers"]).status_code == 422
    assert client.get(JOBS, params={"cursor": "garbage"}, headers=user["headers"]).status_code == 422


def test_result_unavailable_until_succeeded(client, user):
    job_id = submit(client, user["headers"])["job_id"]
    response = client.get(f"{JOBS}/{job_id}/result", headers=user["headers"])
    assert response.status_code == 409


def test_scheduled_at_is_respected(client, user):
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    body = submit(client, user["headers"], scheduled_at=later)
    assert datetime.fromisoformat(body["scheduled_at"]) > datetime.now(timezone.utc)


def test_list_job_types(client, user):
    types = client.get(f"{JOBS}/types", headers=user["headers"]).json()
    assert "generate_report" in types


def test_rate_limit_on_submission(client, user, settings):
    settings.rate_limit_submit_per_minute = 2
    codes = [client.post(JOBS, json={"job_type": "simulated_work"}, headers=user["headers"]).status_code
             for _ in range(3)]
    assert codes == [201, 201, 429]


def test_database_failure_returns_sanitised_error_and_persists_nothing(redis_client, user, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    import main

    def broken_commit(self):
        raise OperationalError("COMMIT", {}, Exception("connection to postgresql://u:secret@db lost"))

    monkeypatch.setattr(Session, "commit", broken_commit)
    with TestClient(main.app, raise_server_exceptions=False) as raw_client:
        response = raw_client.post(JOBS, json={"job_type": "simulated_work"}, headers=user["headers"])
    monkeypatch.undo()

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
