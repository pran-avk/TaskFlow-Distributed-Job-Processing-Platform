# TaskFlow — Distributed Job Processing Platform

TaskFlow is a background job processing platform built with **Python, FastAPI, PostgreSQL, Redis and Docker**. Clients submit jobs over a REST API and get a job ID back immediately. Independent workers execute the jobs, and clients poll for status and results. Failures are retried with backoff, and work lost to crashes is recovered automatically.

The project is about how a backend behaves under concurrency and failure, not just about running tasks in the background. It covers atomic job claiming, a transactional outbox, execution leases with fencing, idempotent submission, at-least-once delivery, and recovery from dead workers and lost queue messages.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Quick Start](#quick-start)
4. [Using the Dashboard](#using-the-dashboard) · [Using the API](#using-the-api)
5. [API Reference](#api-reference)
6. [Job Types](#job-types)
7. [Job Lifecycle](#job-lifecycle)
8. [How Reliability Works](#how-reliability-works)
9. [Database Schema](#database-schema)
10. [Security](#security)
11. [Observability](#observability)
12. [Configuration](#configuration)
13. [Testing](#testing)
14. [Performance and Recovery Results](#performance-and-recovery-results)
15. [Project Structure](#project-structure)
16. [Deployment](#deployment)
17. [Design Trade-offs](#design-trade-offs)
18. [Limitations and Future Work](#limitations-and-future-work)

---

## Features

**Job management**
- Submit, inspect, list (cursor pagination, status/type filters), cancel and retry jobs
- Idempotent submission with the `Idempotency-Key` header. Replays return the original job, and a conflicting body returns `409`
- Priorities from 0 to 9, mapped to three Redis queues and drained by weighted random choice, so low-priority work is never starved
- Delayed execution with `scheduled_at`
- Full attempt history per job: worker, duration, error type and sanitized message

**Reliable execution**
- Jobs are persisted in PostgreSQL **before** dispatch, through a transactional outbox
- Atomic job claiming with conditional updates. Exactly one worker wins, even with duplicate messages
- Execution leases with heartbeats. Every write is fenced on the attempt number, so a stale worker can never overwrite a newer result
- Exponential backoff with jitter, a `DEAD_LETTER` state for exhausted retries, and a `FAILED` state for permanent errors
- Per-handler execution timeouts and cooperative cancellation of running jobs
- Graceful worker shutdown: the worker stops claiming, finishes in-flight work within a grace period, and leaves anything unfinished retryable
- Automatic recovery of expired leases, lost Redis messages and messages held by dead workers

**Web dashboard**
- Built-in UI at `/ui`: sign in, submit jobs through forms generated from each job type's schema, watch jobs update live, inspect results and attempts, cancel and retry, and see queue and worker health

**Production engineering**
- JWT authentication with access and refresh tokens, Argon2 password hashing, and tenant isolation (users see only their own jobs)
- Per-user rate limits on submission and retry, plus request-size and payload-size limits
- Allowlisted job types with strict payload schemas. No arbitrary code execution
- Structured JSON logs with secret redaction and log-injection protection
- Prometheus metrics, alert rules and a provisioned Grafana dashboard
- Alembic migrations, Docker images, a Docker Compose stack, and GitHub Actions CI against both SQLite and PostgreSQL

---

## Architecture

```text
                              CLIENTS
                                 |
                                 v
                    +--------------------------+
                    |      FastAPI  (API)      |
                    | auth · validation · rate |
                    | limits · idempotency     |
                    +--------------------------+
                                 |  one transaction:
                                 |  job row + outbox event
                                 v
   +--------------------------------------------------------------+
   |                         PostgreSQL                           |
   | jobs · job_attempts · outbox_events · audit_logs · users     |
   |                (authoritative job state)                     |
   +--------------------------------------------------------------+
        ^            |                         ^              ^
        |            | due events              | claim /      | expired leases,
        |            v                         | outcome      | stale jobs
        |   +-----------------+                |              |
        |   |   Dispatcher    |                |     +-----------------+
        |   | (outbox → Redis)|                |     |    Scheduler    |
        |   +-----------------+                |     |   (recovery)    |
        |            | job ids                 |     +-----------------+
        |            v                         |              |
        |   +-----------------+       +-----------------+     |
        |   |      Redis      | ----> |  Worker 1..N    |     |
        |   | high/normal/low | LMOVE | handler threads |     |
        |   | queues, worker  | <---- | lease heartbeat |     |
        |   | heartbeats      |  ack  +-----------------+     |
        |   +-----------------+                               |
        |            ^----------- orphaned messages ----------+
        |
   Prometheus  <---- /api/v1/metrics, worker/dispatcher/scheduler :9101
        |
     Grafana
```

### Processes

| Process | Command | Responsibility | Scaling |
|---|---|---|---|
| API | `uvicorn main:app` | Auth, validation, persistence, status queries | Stateless, run replicas behind a load balancer |
| Dispatcher | `python -m scheduler.dispatcher` | Publishes due outbox events to Redis | Safe to run several (`FOR UPDATE SKIP LOCKED`) |
| Scheduler | `python -m scheduler.scheduler` | Recovers expired leases, lost messages and dead workers; purges old outbox rows | Safe to run several (all writes are conditional) |
| Worker | `python -m workers.runner` | Claims and executes jobs, renews leases, records outcomes | Scale freely; `WORKER_CONCURRENCY` slots each |

**Redis vs PostgreSQL:** Redis queue entries hold job IDs only. PostgreSQL is the single source of truth, and a worker always re-reads and claims the job there before running it. Redis can therefore lose or duplicate messages without corrupting job state.

---

## Quick Start

### Option A — full stack with Docker Compose

Requirements: Docker with Docker Compose.

```bash
git clone <your-repository-url>
cd TaskFlow-Distributed-Job-Processing-Platform
cp .env.example .env
# edit .env and set JWT_SECRET_KEY, e.g.:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up --build
```

This starts PostgreSQL, Redis, a one-off migration job, the API, the dispatcher, the scheduler, two workers, Prometheus and Grafana.

| Service | URL |
|---|---|
| **Dashboard** | http://localhost:8000/ui |
| API + interactive docs | http://localhost:8000/docs |
| Prometheus | http://localhost:9090 |
| Grafana (admin / admin) | http://localhost:3000 → *TaskFlow Overview* |

Scale workers with `docker compose up -d --scale worker=5`.

### Option B — run the processes yourself

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/), plus PostgreSQL and Redis (or `docker compose up -d postgres redis`).

```bash
uv sync
cp .env.example .env               # set JWT_SECRET_KEY, DATABASE_URL, REDIS_URL
uv run alembic upgrade head
```

Then start each process in its own terminal:

```bash
uv run uvicorn main:app --reload           # API on http://localhost:8000
uv run python -m scheduler.dispatcher      # outbox → Redis
uv run python -m scheduler.scheduler       # recovery
uv run python -m workers.runner            # worker (start as many as you like)
```

`DATABASE_URL` defaults to a local SQLite file (`sqlite:///./taskflow.db`), which is fine for trying things out. Use PostgreSQL for anything concurrent.

### Try it

Open **http://localhost:8000/ui**, create an account, and submit a job from the **New job** form. The jobs table refreshes every 2 seconds, so you can watch the status move from `PENDING` to `SUCCEEDED`. Click a job to see its result and every attempt, or to cancel or retry it.

To load a mix of sample jobs, including ones that retry and succeed, end up in dead-letter, or fail permanently:

```bash
uv run python scripts/create_test_jobs.py
```

---

## Using the Dashboard

The dashboard at `/ui` is a single page served by the API, with no build step and no external scripts.

| Area | What you can do |
|---|---|
| Sign in / Create account | Uses `/auth/login` and `/auth/signup`; access tokens refresh automatically |
| New job | Pick a job type and fill in a form generated from its payload schema, or switch to raw JSON. Options: priority, max attempts, run-at time, idempotency key |
| Jobs | Live table with status badges, filters by status and type, and paging |
| Job details | Result, last error, payload, every attempt (worker, duration, error), plus **Cancel**, **Retry** and **Copy ID** |
| System | Queue depths, in-flight messages, outbox backlog, and live/offline workers |

The page is served with a strict Content-Security-Policy (own scripts and styles only). All job data is rendered as text, never as HTML.

---

## Using the API

A complete session with `curl`:

```bash
# 1. Create an account (or log in with POST /auth/login)
curl -s -X POST localhost:8000/auth/signup -H 'Content-Type: application/json' -d '{
  "email": "me@example.com", "password": "a-strong-password",
  "full_name": "Me", "phone_number": "5550100"
}'
# -> {"user_id": 1, "ascess_token": "<access>", "refresh_token": "<refresh>", ...}

TOKEN=<access>

# 2. Submit a job
curl -s -X POST localhost:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: report-2026-001" \
  -H 'Content-Type: application/json' -d '{
  "job_type": "generate_report",
  "payload": {"report_id": "report-123", "format": "csv"},
  "priority": 5,
  "max_attempts": 3,
  "scheduled_at": null
}'
# 201 -> {"job_id": "6fd3f5b7-...", "status": "PENDING", "created_at": "...", "scheduled_at": "..."}

# 3. Check status
curl -s localhost:8000/api/v1/jobs/$JOB_ID -H "Authorization: Bearer $TOKEN"

# 4. Fetch the result once it has SUCCEEDED
curl -s localhost:8000/api/v1/jobs/$JOB_ID/result -H "Authorization: Bearer $TOKEN"
# -> {"job_id": "...", "status": "SUCCEEDED",
#     "result": {"file_name": "report-123.csv", "records_processed": 1500, ...}}

# 5. When the access token expires (30 min), refresh it
curl -s -X POST localhost:8000/auth/refresh -H 'Content-Type: application/json' \
  -d '{"refresh_token": "<refresh>"}'
```

Sending the same request again with the same `Idempotency-Key` returns `200` and the original job. Reusing the key with a different body returns `409 Conflict`.

---

## API Reference

All job and operations endpoints live under `/api/v1`. Interactive OpenAPI docs are served at `/docs`.

### Authentication

| Method | Endpoint | Body | Returns |
|---|---|---|---|
| `POST` | `/auth/signup` | `email`, `password` (≥ 8 chars), `full_name`, `phone_number` | user info + `ascess_token`, `refresh_token` |
| `POST` | `/auth/login` | `email`, `password` | `access_token`, `refresh_token` |
| `POST` | `/auth/refresh` | `refresh_token` | new `access_token` |

Send the access token as `Authorization: Bearer <token>`. Access tokens last 30 minutes and refresh tokens 7 days. A refresh token is rejected where an access token is expected, and vice versa.

### Jobs (authentication required)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/jobs` | Submit a job. Optional `Idempotency-Key` header. `201` new, `200` idempotent replay |
| `GET` | `/api/v1/jobs` | List your jobs. Query: `status`, `job_type`, `limit` (1–100), `cursor` |
| `GET` | `/api/v1/jobs/types` | List the allowlisted job types |
| `GET` | `/api/v1/jobs/types/schemas` | Payload JSON schema and timeout for each job type (used by the dashboard to build forms) |
| `GET` | `/api/v1/jobs/{job_id}` | Job details, including payload, timestamps, `last_error` and `result` |
| `GET` | `/api/v1/jobs/{job_id}/attempts` | Execution history (one row per attempt) |
| `GET` | `/api/v1/jobs/{job_id}/result` | Result of a `SUCCEEDED` job (`409` otherwise) |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Cancel now if not running; otherwise request cooperative cancellation |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Re-run a `FAILED`, `DEAD_LETTER` or `CANCELLED` job (audited) |

**Submit body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `job_type` | string | — | Must be a registered type |
| `payload` | object | `{}` | Validated against the job type's schema; ≤ 64 KB |
| `priority` | int | `5` | 0 (lowest) – 9 (highest) |
| `max_attempts` | int | `3` | 1 – 20 |
| `scheduled_at` | datetime | `null` | Earliest execution time; UTC if no offset |

**Pagination:** responses contain `items` and `next_cursor`. Pass `next_cursor` back as `cursor` to get the next page. The cursor is keyset-based, so pages stay stable while new jobs arrive.

### Operations

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/health/live` | none | Liveness |
| `GET` | `/api/v1/health/ready` | none | Checks PostgreSQL and Redis; `503` if either is down |
| `GET` | `/api/v1/queues/stats` | user | Queue depths, in-flight messages, outbox backlog, job counts by status |
| `GET` | `/api/v1/workers` | user | Registered workers, heartbeat age, health, active jobs, totals |
| `GET` | `/api/v1/metrics` | `METRICS_TOKEN` | Prometheus metrics; disabled (`404`) when no token is configured |

### Error responses

| Status | Meaning |
|---|---|
| `401` | Missing, invalid or expired token |
| `404` | Job does not exist **or belongs to another user** (existence is never leaked) |
| `409` | Idempotency conflict, or the job's state does not allow the operation |
| `413` | Request body larger than `MAX_REQUEST_BYTES` |
| `422` | Unknown job type, invalid payload or invalid parameters |
| `429` | Rate limit exceeded (see `Retry-After`) |
| `500` | Unexpected error; details are logged server-side, never returned |

---

## Job Types

Clients choose from an allowlist of registered handlers. They can never submit code. Each handler declares a payload schema (unknown fields are rejected) and a timeout.

| Job type | Payload | Result | Timeout |
|---|---|---|---|
| `generate_report` | `report_id`, `format` (`csv`/`json`), `rows` (1–100 000, default 1500) | `file_name`, `records_processed`, `size_bytes`, `sha256` | 120 s |
| `process_file` | `file_name`, `content` (≤ 50 000 chars), `operations` (`line_count`, `word_count`, `checksum`, `top_words`) | Requested statistics | 60 s |
| `send_notification` | `notification_id`, `channel` (`email`/`sms`/`webhook`), `recipient`, `message` | `delivered`, `deduplicated` | 30 s |
| `simulated_work` | `duration_ms`, `fail_until_attempt`, `failure` (`transient`/`permanent`) | `slept_ms`, `attempt` | 60 s |

`send_notification` shows how to write an idempotent handler. It records each `notification_id` in Redis with `SET NX`, so a retried or duplicated job never sends twice. `simulated_work` is for demos, benchmarks and failure testing.

### Adding a job type

```python
# workers/handlers/resize_image.py
from pydantic import BaseModel, ConfigDict, Field
from workers.context import JobContext
from workers.errors import PermanentError
from workers.registry import job_handler

class ResizeImagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_id: str
    width: int = Field(gt=0, le=4096)

@job_handler("resize_image", ResizeImagePayload, timeout_seconds=90)
def resize_image(payload: ResizeImagePayload, ctx: JobContext) -> dict:
    ...
    ctx.check()          # honour cancellation / timeout / shutdown in long loops
    ...
    return {"image_id": payload.image_id, "width": payload.width}
```

Import the module in `workers/handlers/__init__.py`. Raise `PermanentError` for failures that retrying cannot fix and `RetryableError` for transient ones. Other exceptions are treated as retryable.

---

## Job Lifecycle

```text
                 PENDING ──────────────┐
                    │ dispatcher       │
                    v                  │
                 QUEUED ───────────────┤
                    │ worker claim     │  cancel
                    v                  │
                 RUNNING ──────────────┼──────────> CANCELLED
                /   |    \             │
       success /    |     \ permanent  │
              v     |      v error     │
       SUCCEEDED    |     FAILED       │
                    | retryable error  │
                    v                  │
     attempts left? ── no ──> DEAD_LETTER
                    │ yes
                    v
               RETRY_WAIT ─────────────┘
                    │ backoff elapsed
                    └──────> QUEUED

   FAILED / DEAD_LETTER / CANCELLED ── manual retry ──> PENDING
```

| State | Meaning |
|---|---|
| `PENDING` | Persisted, waiting for dispatch (or for `scheduled_at`) |
| `QUEUED` | Published to Redis |
| `RUNNING` | Claimed by a worker holding a lease |
| `RETRY_WAIT` | Failed with a retryable error; waiting for backoff |
| `SUCCEEDED` | Completed; result stored |
| `FAILED` | Permanent (non-retryable) failure |
| `DEAD_LETTER` | Retryable failures exhausted `max_attempts` |
| `CANCELLED` | Cancelled before or during execution |

Transitions are defined in `jobs/state.py` and enforced by conditional `UPDATE ... WHERE status = ...` statements, so concurrent requests cannot produce illegal transitions. Cancelling a running job sets `cancel_requested`. The worker sees it at its next lease renewal, and the handler stops at its next `ctx.check()`. A manual retry is audited and grants at least one more attempt.

**Retry backoff:** `delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY × 2^(attempt − 1)) + uniform(0, RETRY_JITTER)`. The defaults give 1 s, 2 s, 4 s, … capped at 5 minutes.

---

## How Reliability Works

TaskFlow guarantees **at-least-once execution**. Every eligible job eventually runs, but a crash at the wrong moment can make a handler run twice. Handlers whose side effects must not repeat should be idempotent, as `send_notification` is. Exactly-once execution of arbitrary side effects is not possible without the downstream system's cooperation.

### 1. Persist before dispatch: the transactional outbox

`POST /jobs` writes the job row **and** an `outbox_events` row in one transaction. Nothing touches Redis during the request. The dispatcher then:

1. Locks a batch of due events (`FOR UPDATE SKIP LOCKED`) and moves their jobs `PENDING/RETRY_WAIT → QUEUED`, then commits.
2. Pushes the job IDs to the priority queues.
3. Marks the events published.

If step 2 fails (Redis down), the events stay unpublished and are retried. If the dispatcher dies between steps 2 and 3, the events are published again, and the duplicates are harmless (see 3). Scheduled jobs and retries use the same mechanism, through the event's `available_at`.

### 2. Reliable consumption

A worker moves a message from the queue into its own processing list (`LMOVE`). It removes the message (the "ack") only **after** the outcome is stored in PostgreSQL. If the worker dies, its heartbeat key expires, and the scheduler moves the message back to the queue.

### 3. Atomic claiming

```sql
UPDATE jobs SET status='RUNNING', attempt_count=attempt_count+1, worker_id=:me, lease_expires_at=...
WHERE id=:id AND status='QUEUED' AND attempt_count=:seen
```

Only one worker gets `rowcount = 1`. Every other copy of the message, whether a duplicate publish, a requeue or a stale entry, finds the job not `QUEUED` and is simply acknowledged.

### 4. Leases and fencing

While a handler runs, the worker renews the job's lease every `WORKER_HEARTBEAT_INTERVAL_SECONDS`. If the worker dies or stalls, the scheduler finds `RUNNING` jobs with an expired lease. It records the attempt as `LOST` and moves the job to `RETRY_WAIT`, or to `DEAD_LETTER` if no attempts are left.

Lease expiry does not prove the old worker has stopped. Every later write is therefore fenced on `(status = RUNNING, attempt_count, worker_id)`. A worker that comes back after losing its lease cannot record a result over the newer attempt; it sees `rowcount = 0` and discards its outcome.

### 5. Timeouts, cancellation and shutdown

Handlers run in a thread and receive a `JobContext`. At the timeout, cancellation request or shutdown, the context tells the handler to stop, and `ctx.check()` / `ctx.sleep()` raise inside it. A handler that ignores the signal for 2 more seconds is abandoned, and the attempt is recorded as `TIMED_OUT`. Any result it produces later is rejected by the fence.

On `SIGTERM` a worker stops claiming, finishes in-flight jobs within `WORKER_SHUTDOWN_GRACE_SECONDS`, then interrupts the rest. Interrupted jobs are recorded as retryable `WorkerShutdown` failures.

### 6. Recovery loop (scheduler)

| Problem | Detection | Action |
|---|---|---|
| Worker crashed mid-job | `RUNNING` with `lease_expires_at < now` | Attempt → `LOST`; job → `RETRY_WAIT` / `DEAD_LETTER` |
| Worker crashed holding a message | Worker in registry, heartbeat key expired | Move its processing list back to the queue |
| Message lost (Redis restart, eviction) | `QUEUED` longer than `QUEUED_REDISPATCH_AFTER_SECONDS` with no pending event | New outbox event → re-dispatched |
| Outbox growth | Published events older than `OUTBOX_RETENTION_DAYS` | Deleted hourly |

Each of these scenarios has a regression test in `tests/reliability/`.

---

## Database Schema

Managed by Alembic (`alembic/versions/`).

**`jobs`** holds the current state of each job: `id` (UUID), `tenant_id` (→ `users.id`), `job_type`, `payload` (JSONB), `status`, `priority`, `max_attempts`, `attempt_count`, `scheduled_at`, `created_at`, `updated_at`, `queued_at`, `started_at`, `completed_at`, `result` (JSONB), `last_error` (JSONB), `idempotency_key`, `request_hash`, `cancel_requested`, `worker_id`, `lease_expires_at`.
Constraints and indexes: unique `(tenant_id, idempotency_key)`; checks on priority range and attempt counts; indexes on `(tenant_id, created_at)`, `(status, scheduled_at)` and `(status, lease_expires_at)`.

**`job_attempts`** has one row per execution: `job_id`, `attempt_number` (unique per job), `worker_id`, `status` (`RUNNING`/`SUCCEEDED`/`FAILED`/`TIMED_OUT`/`LOST`/`CANCELLED`), `started_at`, `finished_at`, `error_type`, `error_message` (sanitized), `duration_ms`.

**`outbox_events`** holds dispatch intents: `job_id`, `event_type`, `payload`, `created_at`, `available_at`, `published_at`, `publish_attempts`, `last_error`.

**`audit_logs`** records sensitive actions (manual retry, cancellation): `actor_user_id`, `action`, `job_id`, `details`, `created_at`.

**`users`** stores accounts: `id`, `full_name`, `email` (unique), `hashed_password` (Argon2), `phone_number`.

**Idempotency** uses a single mechanism: the unique `(tenant_id, idempotency_key)` constraint plus a SHA-256 `request_hash` of the normalized request. If two identical requests race, the loser hits the constraint, rolls back and returns the winner's job.

---

## Security

- **Authentication:** short-lived JWT access tokens and longer-lived refresh tokens, each typed so one cannot be used as the other; Argon2 password hashing.
- **Tenant isolation:** every job query is scoped to the caller. Other tenants' jobs return `404`.
- **No code execution:** only allowlisted job types run, and payloads are strictly validated (`extra="forbid"`).
- **Abuse limits:** per-user rate limits on submit and retry; 1 MB request-body limit; 64 KB payload limit. The rate limiter fails open if Redis is down, so a cache outage cannot take the API down.
- **Sanitized errors:** unhandled exceptions return a generic `500`; stack traces go only to the logs.
- **Safe logging:** bearer tokens, `token=` query parameters, passwords and database URL credentials are redacted, and newlines are escaped to prevent log injection.
- **Protected metrics:** `/api/v1/metrics` requires a separate bearer token and is disabled when none is set. Metric labels never include job IDs or payload data.
- **Audit trail:** manual retries and cancellations are written to `audit_logs`.
- **Secrets:** read from the environment or `.env`, which is git-ignored. Containers run as a non-root user, and Compose binds PostgreSQL, Redis, Prometheus and Grafana to `127.0.0.1` only.

In production, also terminate TLS at the load balancer and restrict database and Redis network access.

---

## Observability

### Metrics

| Metric | Type | Labels | Source |
|---|---|---|---|
| `taskflow_jobs_submitted_total` | counter | `job_type` | API |
| `taskflow_jobs_completed_total` | counter | `job_type`, `outcome` | worker, scheduler |
| `taskflow_job_duration_seconds` | histogram | `job_type` | worker |
| `taskflow_queue_wait_seconds` | histogram | `job_type` | worker |
| `taskflow_job_retries_total` | counter | `job_type` | worker, scheduler |
| `taskflow_worker_active_jobs` | gauge | — | worker |
| `taskflow_queue_depth` | gauge | `queue` | API (read at scrape time) |
| `taskflow_outbox_pending_events` | gauge | — | API (read at scrape time) |
| `taskflow_worker_heartbeat_age_seconds` | gauge | `worker_id` | API (read at scrape time) |
| `taskflow_outbox_published_total` | counter | — | dispatcher |
| `taskflow_recovered_total` | counter | `kind` | scheduler |

The API serves metrics at `/api/v1/metrics`. Workers, the dispatcher and the scheduler each expose `:9101/metrics` on the internal network. Prometheus discovers worker replicas through Docker DNS.

### Dashboard and alerts

Grafana is provisioned with a **TaskFlow Overview** dashboard: queue depth, outbox backlog, active jobs, healthy workers, submission and outcome rates, p50/p95 duration and queue wait, retries, heartbeat ages and recovery actions.

`monitoring/alerts.yml` defines alerts for a sustained queue backlog, slow queue wait, high failure rate, no healthy workers, outbox backlog, repeated lease expiries and down targets. The thresholds are starting points; tune them from measured behavior.

### Logs

Logs are JSON lines, one object per line, with `ts`, `level`, `logger` and `message`, plus `job_id`, `job_type`, `worker_id`, `attempt` and `status` where relevant. Set `LOG_JSON=false` for human-readable output in development.

---

## Configuration

All settings come from environment variables or `.env` (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `JWT_SECRET_KEY` | **required** | Signing key for tokens |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |
| `DATABASE_URL` | `sqlite:///./taskflow.db` | e.g. `postgresql+psycopg://user:pass@host:5432/taskflow` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `METRICS_TOKEN` | unset | Bearer token for `/api/v1/metrics` (unset disables it) |
| `MAX_REQUEST_BYTES` | `1048576` | Maximum request body |
| `MAX_PAYLOAD_BYTES` | `65536` | Maximum serialized job payload |
| `RATE_LIMIT_SUBMIT_PER_MINUTE` | `60` | Per-user job submissions per minute (0 = off) |
| `RATE_LIMIT_RETRY_PER_MINUTE` | `20` | Per-user manual retries per minute (0 = off) |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | Backoff base |
| `RETRY_MAX_DELAY_SECONDS` | `300.0` | Backoff cap |
| `RETRY_JITTER_SECONDS` | `1.0` | Maximum random jitter added |
| `WORKER_CONCURRENCY` | `4` | Job slots per worker process |
| `WORKER_POLL_INTERVAL_SECONDS` | `0.5` | Idle poll interval |
| `WORKER_LEASE_SECONDS` | `30` | Lease length; the main factor in crash-recovery time |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `5.0` | Lease renewal / heartbeat interval |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30.0` | Time to finish in-flight work on shutdown |
| `QUEUE_WEIGHTS` | `[6,3,1]` | Relative drain weights for high/normal/low queues |
| `METRICS_PORT` | `9101` | Metrics port for worker/dispatcher/scheduler (0 = off) |
| `DISPATCHER_BATCH_SIZE` | `100` | Outbox events per batch |
| `DISPATCHER_INTERVAL_SECONDS` | `0.5` | Dispatcher idle interval |
| `SCHEDULER_INTERVAL_SECONDS` | `5.0` | Recovery loop interval |
| `QUEUED_REDISPATCH_AFTER_SECONDS` | `300` | When a still-`QUEUED` job is treated as lost |
| `OUTBOX_RETENTION_DAYS` | `7` | Retention for published outbox events |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_JSON` | `true` | JSON or plain-text logs |

---

## Testing

```bash
uv sync
uv run python -m pytest
```

By default the suite uses a temporary SQLite database and an in-memory Redis, so no services are needed. To run the same tests against real infrastructure:

```bash
TEST_DATABASE_URL=postgresql+psycopg://taskflow:taskflow@localhost:5432/taskflow \
TEST_REDIS_URL=redis://localhost:6379/0 \
uv run python -m pytest
```

There are **117 tests**, and they pass on both SQLite and PostgreSQL 16.

| Suite | Covers |
|---|---|
| `tests/unit` | State transitions, backoff and jitter, retry/dead-letter decisions, failure classification, handler registry and payload validation, priority queues and weighting, log sanitization |
| `tests/integration` | Dashboard assets and security headers, job-type schemas, auth (expired, invalid and wrong-type tokens; refresh), tenant isolation, submission validation, size and rate limits, idempotency, pagination and filters, end-to-end execution of every handler, retries, dead-letter, manual retry, scheduling, priority routing, cancellation (pending, queued, running), timeouts, health/metrics/stats/workers endpoints, rollback on database failure |
| `tests/reliability` | Duplicate queue messages, repeated dispatch, Redis down during dispatch and during submission, PostgreSQL down during claim, expired leases and fencing of stale workers, dead-worker message recovery, lost Redis messages, concurrent claims, concurrent retries, concurrent duplicate submissions, graceful shutdown inside and beyond the grace period |
| `tests/load` | Locust workload (`uv sync --extra load`, then `uv run locust -f tests/load/locustfile.py --host http://localhost:8000`) |

**CI** (`.github/workflows/ci.yml`) runs the suite on SQLite and on PostgreSQL + Redis service containers. It also checks that migrations apply, roll back and match the models (`alembic check`), builds both Docker images and validates the Compose file.

---

## Performance and Recovery Results

Measured with `scripts/benchmark.py`, with every component running as a separate OS process:

| Setting | Value |
|---|---|
| Machine | Windows 11, Intel Core (Raptor Lake), single host |
| Database | PostgreSQL 16.2 (local), psycopg pure-Python driver |
| Queue | Python Redis emulator (`fakeredis` TCP server), not real Redis |
| Topology | 1 API, 1 dispatcher, 1 scheduler, 2 workers × 4 slots |
| Workload | 1,000 `simulated_work` jobs of 50 ms, 20 concurrent clients |

| Metric | Run 1 | Run 2 |
|---|---|---|
| Submission errors | 0 | 0 |
| Submission throughput | 214 jobs/s | 199 jobs/s |
| Submit latency p50 / p95 / p99 | 90 / 126 / 157 ms | 96 / 136 / 194 ms |
| Completion throughput | 106 jobs/s | 110 jobs/s |
| Queue wait p50 / p95 | 1.89 / 2.30 s | 1.91 / 2.26 s |
| Jobs succeeded | 1,000 / 1,000 | 1,000 / 1,000 |

Eight slots running 50 ms jobs can complete at most 160 jobs/s. The measured ≈108/s (about 68% of that) is limited by per-job database round trips through the pure-Python driver and by the Python Redis emulator. Submissions arrived about twice as fast as they were processed, so queue wait grew to around 2 s by the end of the burst. Real Redis and the compiled psycopg driver should do better.

**Crash recovery.** A worker was hard-killed (no graceful shutdown) while running a 4 s job, with `WORKER_LEASE_SECONDS=6`:

| Step | Result |
|---|---|
| Attempt 1 | `LOST` (`LeaseExpired`), recorded by the scheduler |
| Attempt 2 | `SUCCEEDED` on the surviving worker |
| Kill to success | 11.0 s (≈ 6 s lease + backoff + 4 s re-run) |
| Dead worker | Removed from the registry; its in-flight message requeued |

Recovery time is dominated by `WORKER_LEASE_SECONDS`, which trades recovery speed against tolerance for slow heartbeats.

Reproduce against the Compose stack:

```bash
uv run python scripts/benchmark.py --base-url http://localhost:8000 --jobs 1000 --concurrency 20 --duration-ms 50
```

---

## Project Structure

```text
.
├── main.py                        # FastAPI app: routers, dashboard, size limit, sanitized errors, metrics
├── frontend/                      # Dashboard: index.html, app.js, styles.css (served at /ui)
├── database.py                    # Engine, sessions, UTC datetime column type
├── core/
│   ├── config.py                  # Settings from environment / .env
│   ├── security.py                # Bearer auth dependency, metrics token check
│   ├── rate_limit.py              # Per-user Redis rate limiter
│   └── logging.py                 # JSON logs, redaction, log-injection guard
├── users/                         # Signup, login, refresh; User model
├── jwtauth/auth/                  # Token creation and verification
├── jobs/
│   ├── state.py                   # States and allowed transitions
│   ├── models/                    # Job, JobAttempt, OutboxEvent, AuditLog
│   ├── schemas/job.py             # Request/response models
│   ├── api/jobs.py                # /api/v1/jobs routes
│   └── service/
│       ├── job_service.py         # Submit (idempotent), list, cancel, manual retry
│       ├── execution_service.py   # Claim, lease renewal, fenced outcome writes
│       ├── retry_service.py       # Backoff and retry decisions
│       ├── dispatch_service.py    # Outbox → Redis
│       └── recovery_service.py    # Leases, lost messages, dead workers, cleanup
├── taskqueue/                     # Redis client, priority queues, fetch/ack
├── workers/
│   ├── runner.py                  # Worker process
│   ├── registry.py                # Job-type allowlist and payload validation
│   ├── context.py, errors.py      # Stop signals; failure classification
│   └── handlers/                  # generate_report, process_file, send_notification, simulated_work
├── scheduler/
│   ├── dispatcher.py              # Outbox publisher process
│   ├── scheduler.py               # Recovery process
│   └── loop.py                    # Shared run loop with backoff and signal handling
├── ops/
│   ├── api.py                     # Health, queue stats, workers, metrics endpoints
│   └── metrics.py                 # Prometheus metric definitions
├── alembic/                       # Migrations
├── tests/                         # unit, integration, reliability, load
├── scripts/                       # benchmark.py, create_test_jobs.py
├── monitoring/                    # prometheus.yml, alerts.yml, Grafana provisioning + dashboard
├── Dockerfile                     # API / dispatcher / scheduler image
├── docker/worker.Dockerfile       # Worker image
├── compose.yaml                   # Full local stack
├── .env.example
└── .github/workflows/ci.yml
```

---

## Deployment

```text
           Internet / clients
                  |
           HTTPS load balancer
                  |
          FastAPI replicas (N)
           /               \
     PostgreSQL           Redis (AOF)
           \               /
   Dispatcher · Scheduler · Workers (M)
                  |
         Prometheus → Grafana
```

- Run migrations (`alembic upgrade head`) as a one-off step before starting new application versions, as the Compose `migrate` service does.
- Run one uvicorn process per API container and scale with replicas. Prometheus counters are per-process.
- Give workers a stop grace period longer than `WORKER_SHUTDOWN_GRACE_SECONDS` (Compose uses 35 s for 25 s).
- Redis runs with AOF persistence. If Redis data is lost anyway, the scheduler re-dispatches affected jobs from PostgreSQL.
- Back up PostgreSQL, since it holds all job state. Redis holds no unrecoverable data.
- Use `credentials_file` (from a secret) instead of the inline development token in `monitoring/prometheus.yml`.

---

## Design Trade-offs

**PostgreSQL and Redis.** PostgreSQL provides durable records, transactions, constraints and queryable history. Redis provides fast queueing and coordination. The two cannot share a transaction, so the outbox bridges them, and the database is the only source of truth.

**Independent workers.** Execution capacity scales separately from the API, and long-running work never blocks request handling.

**At-least-once instead of exactly-once.** A worker can finish an external side effect and crash before recording success. Without cooperation from the downstream system, no platform can tell that apart from a crash before the side effect. TaskFlow makes duplicates safe for its own state (atomic claims, fencing) and gives handlers an idempotency pattern for theirs.

**Threads for handlers.** Threads keep the worker simple and portable. The cost is that Python threads cannot be killed, so timeouts rely on cooperation backed by fencing. CPU-heavy or untrusted handlers would call for process isolation.

**A modular monolith, not microservices.** One codebase runs as four process types: API, dispatcher, scheduler and worker. That gives real separation without the operational cost of extra network boundaries.

**Users as tenants.** Each account owns its jobs, and the tenant key is `users.id`. A multi-user organization model could be added without changing the job tables' design.

---

## Limitations and Future Work

- Recurring (cron-style) jobs
- Workflows with dependencies between jobs
- Per-tenant quotas and priority policies
- WebSocket or webhook status notifications instead of polling
- Artifact storage (e.g. S3) for large job outputs
- Process-isolated execution for CPU-bound or untrusted handlers
- OpenTelemetry tracing across API, dispatcher and workers
- Worker autoscaling from queue depth
- Admin role for cross-tenant operations (queue stats and worker lists are currently available to any authenticated user and contain only aggregate data)

---

**TaskFlow — reliable background execution through durable state, distributed workers and observable processing.**
