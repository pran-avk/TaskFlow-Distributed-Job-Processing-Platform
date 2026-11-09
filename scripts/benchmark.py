"""End-to-end benchmark against a running TaskFlow stack (API + dispatcher + workers).

    python scripts/benchmark.py --base-url http://localhost:8000 --jobs 500 --concurrency 20 --duration-ms 50

Signs up a throwaway user, submits jobs concurrently, then polls until every job
is terminal. Reports submission latency percentiles, submission throughput,
completion throughput and queue wait / end-to-end latency from the job records.
"""
import argparse
import json
import platform
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELLED"}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def authenticate(client: httpx.Client) -> dict:
    email = f"bench-{uuid.uuid4().hex[:10]}@example.com"
    response = client.post("/auth/signup", json={
        "email": email, "password": "benchmark-password", "full_name": "Benchmark", "phone_number": "0000000",
    })
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['ascess_token']}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--jobs", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--duration-ms", type=int, default=50, help="simulated work per job")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    with httpx.Client(base_url=args.base_url, timeout=30, limits=limits) as client:
        headers = authenticate(client)
        body = {"job_type": "simulated_work", "payload": {"duration_ms": args.duration_ms}}

        def submit(_):
            start = time.perf_counter()
            response = client.post("/api/v1/jobs", json=body, headers=headers)
            elapsed = time.perf_counter() - start
            return response.status_code, elapsed, response.json().get("job_id") if response.status_code == 201 else None

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(submit, range(args.jobs)))
        submit_elapsed = time.perf_counter() - started

        job_ids = [job_id for _, _, job_id in results if job_id]
        latencies = [elapsed * 1000 for status, elapsed, _ in results if status == 201]
        errors = sum(1 for status, _, _ in results if status != 201)

        pending = set(job_ids)
        details: dict[str, dict] = {}
        deadline = time.perf_counter() + args.timeout
        while pending and time.perf_counter() < deadline:
            cursor = None
            while True:
                params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
                page = client.get("/api/v1/jobs", params=params, headers=headers).json()
                for item in page["items"]:
                    if item["status"] in TERMINAL and item["job_id"] in pending:
                        pending.discard(item["job_id"])
                        details[item["job_id"]] = item
                cursor = page["next_cursor"]
                if not cursor:
                    break
            if pending:
                time.sleep(0.25)
        total_elapsed = time.perf_counter() - started

        sample = list(details)[: min(len(details), 200)]
        waits, end_to_end = [], []
        for job_id in sample:
            job = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            if job.get("started_at"):
                created = datetime.fromisoformat(job["created_at"])
                waits.append((datetime.fromisoformat(job["started_at"]) - created).total_seconds() * 1000)
                end_to_end.append((datetime.fromisoformat(job["completed_at"]) - created).total_seconds() * 1000)

    outcomes: dict[str, int] = {}
    for item in details.values():
        outcomes[item["status"]] = outcomes.get(item["status"], 0) + 1

    report = {
        "machine": f"{platform.system()} {platform.release()} / {platform.processor() or platform.machine()}",
        "jobs": args.jobs,
        "concurrency": args.concurrency,
        "simulated_duration_ms": args.duration_ms,
        "submission_errors": errors,
        "submission_throughput_per_s": round(len(latencies) / submit_elapsed, 1),
        "submit_latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
        },
        "completed": len(details),
        "incomplete_at_timeout": len(pending),
        "outcomes": outcomes,
        "completion_throughput_per_s": round(len(details) / total_elapsed, 1),
        "queue_wait_ms": {"p50": round(percentile(waits, 50), 1), "p95": round(percentile(waits, 95), 1)},
        "end_to_end_ms": {"p50": round(percentile(end_to_end, 50), 1), "p95": round(percentile(end_to_end, 95), 1)},
        "wall_clock_s": round(total_elapsed, 2),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
