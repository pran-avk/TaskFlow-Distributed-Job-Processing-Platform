from datetime import datetime, timedelta, timezone

import jwt

from core.config import get_settings
from tests.helpers import signup, submit


def test_jobs_require_authentication(client):
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.post("/api/v1/jobs", json={"job_type": "simulated_work"}).status_code == 401


def test_invalid_token_rejected(client):
    response = client.get("/api/v1/jobs", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_refresh_token_cannot_be_used_as_access_token(client, user):
    headers = {"Authorization": f"Bearer {user['refresh_token']}"}
    assert client.get("/api/v1/jobs", headers=headers).status_code == 401


def test_expired_token_rejected(client, user):
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(user["user_id"]), "type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.jwt_secret_key, algorithm=settings.jwt_algorithm,
    )
    assert client.get("/api/v1/jobs", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_login_and_refresh_flow(client, user):
    login = client.post("/auth/login", json={"email": user["email"], "password": "correct-horse-battery"})
    assert login.status_code == 200
    refreshed = client.post("/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert refreshed.status_code == 200
    headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert client.get("/api/v1/jobs", headers=headers).status_code == 200


def test_refresh_rejects_access_token(client, user):
    access = user["headers"]["Authorization"].split()[1]
    assert client.post("/auth/refresh", json={"refresh_token": access}).status_code == 401


def test_tenants_cannot_see_each_others_jobs(client):
    alice, bob = signup(client), signup(client)
    job_id = submit(client, alice["headers"])["job_id"]

    for path in (f"/api/v1/jobs/{job_id}", f"/api/v1/jobs/{job_id}/attempts", f"/api/v1/jobs/{job_id}/result"):
        assert client.get(path, headers=bob["headers"]).status_code == 404
    assert client.post(f"/api/v1/jobs/{job_id}/cancel", headers=bob["headers"]).status_code == 404
    assert client.post(f"/api/v1/jobs/{job_id}/retry", headers=bob["headers"]).status_code == 404
    assert client.get("/api/v1/jobs", headers=bob["headers"]).json()["items"] == []
    assert client.get(f"/api/v1/jobs/{job_id}", headers=alice["headers"]).status_code == 200


def test_idempotency_keys_are_scoped_per_tenant(client):
    alice, bob = signup(client), signup(client)
    a = client.post("/api/v1/jobs", json={"job_type": "simulated_work"},
                    headers={**alice["headers"], "Idempotency-Key": "same"})
    b = client.post("/api/v1/jobs", json={"job_type": "simulated_work"},
                    headers={**bob["headers"], "Idempotency-Key": "same"})
    assert a.status_code == b.status_code == 201
    assert a.json()["job_id"] != b.json()["job_id"]
