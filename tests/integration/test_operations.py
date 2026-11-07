from tests.helpers import dispatch, submit


def test_liveness(client):
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}


def test_readiness_checks_dependencies(client):
    body = client.get("/api/v1/health/ready").json()
    assert body == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_metrics_require_token(client):
    assert client.get("/api/v1/metrics").status_code == 401
    assert client.get("/api/v1/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_exposed_with_token(client, user, redis_client):
    submit(client, user["headers"])
    response = client.get("/api/v1/metrics", headers={"Authorization": "Bearer test-metrics-token"})
    assert response.status_code == 200
    text = response.text
    for name in ("taskflow_jobs_submitted_total", "taskflow_queue_depth", "taskflow_outbox_pending_events"):
        assert name in text
    # No unbounded identifiers or payload data in labels.
    assert "job_id" not in text


def test_metrics_disabled_without_configured_token(client, settings):
    settings.metrics_token = None
    assert client.get("/api/v1/metrics").status_code == 404


def test_queue_stats(client, user, redis_client):
    submit(client, user["headers"], priority=9)
    submit(client, user["headers"])
    before = client.get("/api/v1/queues/stats", headers=user["headers"]).json()
    assert before["outbox_pending"] == 2
    assert before["jobs_by_status"] == {"PENDING": 2}

    dispatch(redis_client)
    after = client.get("/api/v1/queues/stats", headers=user["headers"]).json()
    assert after["queues"] == {"high": 1, "normal": 1, "low": 0}
    assert after["outbox_pending"] == 0
    assert after["jobs_by_status"] == {"QUEUED": 2}


def test_ops_endpoints_require_auth(client):
    assert client.get("/api/v1/queues/stats").status_code == 401
    assert client.get("/api/v1/workers").status_code == 401


def test_workers_endpoint_reports_heartbeats(client, user, worker):
    worker.heartbeat()
    workers = client.get("/api/v1/workers", headers=user["headers"]).json()
    assert len(workers) == 1
    assert workers[0]["worker_id"] == "test-worker"
    assert workers[0]["healthy"] is True
    assert workers[0]["state"] == "running"
