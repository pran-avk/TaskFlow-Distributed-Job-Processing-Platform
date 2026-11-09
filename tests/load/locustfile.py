"""Locust workload: `locust -f tests/load/locustfile.py --host http://localhost:8000`

Install with `uv sync --extra load`. Each simulated client signs up once, then
submits jobs and polls their status, mirroring a real client of the API.
"""
import random
import uuid

from locust import HttpUser, between, task


class TaskFlowClient(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        response = self.client.post("/auth/signup", json={
            "email": f"load-{uuid.uuid4().hex[:10]}@example.com",
            "password": "load-test-password",
            "full_name": "Load Test",
            "phone_number": "0000000",
        }, name="/auth/signup")
        self.headers = {"Authorization": f"Bearer {response.json()['ascess_token']}"}
        self.job_ids: list[str] = []

    @task(5)
    def submit_job(self):
        response = self.client.post("/api/v1/jobs", headers=self.headers, json={
            "job_type": "simulated_work",
            "payload": {"duration_ms": random.choice([10, 50, 200])},
            "priority": random.randint(0, 9),
        }, name="/api/v1/jobs [submit]")
        if response.status_code == 201:
            self.job_ids.append(response.json()["job_id"])
            self.job_ids = self.job_ids[-50:]

    @task(3)
    def get_status(self):
        if self.job_ids:
            self.client.get(f"/api/v1/jobs/{random.choice(self.job_ids)}", headers=self.headers,
                            name="/api/v1/jobs/{job_id}")

    @task(1)
    def list_jobs(self):
        self.client.get("/api/v1/jobs?limit=20", headers=self.headers, name="/api/v1/jobs [list]")
