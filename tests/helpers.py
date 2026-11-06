import uuid

from fastapi.testclient import TestClient

from database import SessionLocal
from jobs.service.dispatch_service import dispatch_once
from workers.runner import Worker


def signup(client: TestClient, email: str | None = None) -> dict:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/auth/signup", json={
        "email": email,
        "password": "correct-horse-battery",
        "full_name": "Test User",
        "phone_number": "5550100",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "user_id": body["user_id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {body['ascess_token']}"},
        "refresh_token": body["refresh_token"],
    }


def dispatch(redis_client) -> int:
    return dispatch_once(SessionLocal, redis_client, 100)


def drain(worker: Worker, redis_client, max_rounds: int = 50) -> None:
    """Dispatch and execute until no due work remains."""
    for _ in range(max_rounds):
        dispatch(redis_client)
        if not worker.process_one():
            if dispatch(redis_client) == 0:
                return
    raise AssertionError("work did not settle")


def submit(client: TestClient, headers: dict, job_type: str = "simulated_work", payload: dict | None = None,
           **fields) -> dict:
    body = {"job_type": job_type, "payload": payload if payload is not None else {"duration_ms": 0}, **fields}
    response = client.post("/api/v1/jobs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()
