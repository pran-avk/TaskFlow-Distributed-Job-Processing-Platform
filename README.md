# TaskFlow — Distributed Job Processing Platform

A distributed background job processing platform built with Python, FastAPI, PostgreSQL, Redis, and Docker. TaskFlow allows clients to submit asynchronous jobs, execute them using independent workers, track execution status, retry failures, and monitor processing performance through a REST API.

The project focuses on backend engineering, distributed systems, concurrency control, fault tolerance, and reliable asynchronous processing.

---

## 1. Project Overview

In traditional synchronous applications, a client request may remain blocked while the server performs a long-running operation. This increases response latency, consumes application resources, and makes the system vulnerable to timeouts.

TaskFlow separates job submission from job execution.

Clients submit jobs through a REST API. The API validates requests and persists job metadata in PostgreSQL. Eligible jobs are dispatched through Redis to background workers, which execute them independently and update their execution status.

Clients can retrieve job details, inspect failures, request retries, and monitor queue statistics without waiting for the underlying operation to finish.

### Core Objectives

- Build a reliable asynchronous job processing system.
- Separate API traffic from background execution.
- Support multiple concurrent workers.
- Implement retries and failure recovery.
- Prevent duplicate logical submissions through idempotency.
- Support job priorities, scheduling, and execution timeouts.
- Provide observability into queues, workers, and job execution.
- Demonstrate production-oriented backend engineering practices.

---

## 2. Technology Stack

| Component | Technology | Responsibility |
|---|---|---|
| Programming language | Python | Application and worker implementation |
| API framework | FastAPI | REST API and request validation |
| Database | PostgreSQL | Durable job metadata and execution records |
| Database access | SQLAlchemy | ORM and database operations |
| Schema migrations | Alembic | Version-controlled database migrations |
| Message broker | Redis | Job dispatch and queue coordination |
| Background workers | Python worker processes | Asynchronous job execution |
| Authentication | JWT or secure session-based authentication | Client authentication |
| Testing | Pytest | Unit, integration, and reliability tests |
| Load testing | Locust or k6 | Throughput and latency measurement |
| Containerization | Docker | Consistent runtime environments |
| Local orchestration | Docker Compose | Local multi-service development |
| Metrics | Prometheus | Application and worker metrics |
| Dashboards | Grafana | Operational monitoring |
| CI/CD | GitHub Actions | Automated testing and build checks |

Redis is used for queueing and coordination, not as the sole source of truth for job state. PostgreSQL remains the authoritative store for persisted job metadata and execution results.

---

## 3. High-Level System Architecture

```text
                          CLIENT APPLICATIONS
                                  |
                                  v
                         +-------------------+
                         |   FastAPI API     |
                         | Authentication    |
                         | Validation        |
                         | Rate Limiting     |
                         +-------------------+
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
           +----------------+          +------------------+
           |  PostgreSQL    |          |  Redis           |
           |                |          |                  |
           | Job Metadata   |          | Ready Queues     |
           | Execution Logs |          | Queue Dispatch   |
           | Job Results    |          | Coordination     |
           +----------------+          +------------------+
                    ^                           |
                    |                           v
                    |                +----------------------+
                    |                | Background Workers   |
                    |                |                      |
                    |                | Worker 1             |
                    |                | Worker 2             |
                    |                | Worker N             |
                    |                +----------------------+
                    |                           |
                    +---------------------------+
                                |
                                v
                      Job Status and Results
                                |
                                v
                       Client Status API


                  OBSERVABILITY AND OPERATIONS

               +-------------------------------+
               | Prometheus                    |
               | API, Queue, and Worker Metrics|
               +-------------------------------+
                                |
                                v
               +-------------------------------+
               | Grafana                       |
               | Dashboards and Alerts         |
               +-------------------------------+
```

### Architecture Principles

1. **Separation of concerns:** API processes handle requests; workers perform background execution.
2. **Durable metadata:** Job definitions and state transitions are persisted in PostgreSQL.
3. **Independent scaling:** API replicas and worker processes can scale separately.
4. **Failure isolation:** A failed job should not terminate the worker or prevent unrelated jobs from executing.
5. **Explicit execution guarantees:** The system is designed for at-least-once job delivery, with idempotent handlers used to control duplicate side effects.
6. **Observability:** Queue health, processing latency, retries, and worker failures are measurable.
7. **Security by default:** Clients can access only jobs they are authorized to manage.

---

## 4. Component Architecture

### 4.1 API Service

The API service exposes the REST interface used by clients.

Responsibilities:

- Authenticate clients.
- Validate job submission requests.
- Enforce tenant-level authorization.
- Apply request rate limits.
- Validate job types and execution parameters.
- Persist job metadata.
- Support job status queries.
- Handle retry and cancellation requests.
- Expose health and readiness endpoints.

The API should not execute long-running jobs inside the request-handling process.

A successful submission should return a job identifier promptly, allowing the client to poll for the result.

### 4.2 Job Management Service

The job management service implements application-level job operations.

Responsibilities:

- Create job records.
- Validate permitted state transitions.
- Enforce idempotency rules.
- Calculate retry eligibility.
- Record job completion and failure.
- Manage cancellation requests.
- Store execution metadata.
- Maintain tenant ownership.

Keeping these operations separate from HTTP routing makes the business logic easier to test.

### 4.3 PostgreSQL

PostgreSQL stores durable application data.

Primary responsibilities:

- Persist job definitions and statuses.
- Store job parameters and results.
- Record execution attempts.
- Store idempotency keys.
- Track scheduling metadata.
- Support pagination and filtering.
- Preserve execution history.

PostgreSQL is the authoritative source for a job's persisted status.

### 4.4 Redis

Redis supports fast queueing and coordination.

Responsibilities:

- Hold ready-to-process job references.
- Support priority-specific queues.
- Coordinate worker dispatch.
- Support temporary locks or leases where appropriate.
- Provide queue statistics.

Redis queue entries should generally contain job identifiers rather than entire job records.

The worker retrieves the authoritative job record from PostgreSQL before execution.

**Important:** Redis and PostgreSQL cannot participate in one ordinary atomic transaction. A successful database commit followed by a failed Redis enqueue can leave a persisted job undispatched. TaskFlow must explicitly handle this failure mode.

The recommended design uses a transactional outbox in PostgreSQL and a separate dispatcher to publish pending dispatch events to Redis.

### 4.5 Background Workers

Workers execute the actual job handlers.

Responsibilities:

- Receive job identifiers.
- Retrieve and validate job state.
- Claim eligible jobs.
- Execute registered handlers.
- Enforce execution timeouts.
- Record results.
- Classify failures.
- Schedule eligible retries.
- Acknowledge queue messages.
- Recover safely from shutdowns.

Workers must not trust the queue message alone. Before executing a job, they must verify that the job is still eligible for execution.

### 4.6 Scheduler and Recovery Service

A scheduler manages delayed and recurring work.

Responsibilities:

- Identify jobs whose scheduled execution time has arrived.
- Dispatch eligible jobs.
- Create new instances of recurring jobs.
- Recover expired worker leases.
- Redispatch jobs that remain eligible but were never delivered.
- Detect stale executions.

The scheduler should use PostgreSQL as the source of truth for scheduled jobs and recoverable job states.

### 4.7 Monitoring Service

The monitoring layer exposes operational metrics.

Responsibilities:

- Track queue depth.
- Measure enqueue-to-start latency.
- Measure execution duration.
- Count successes, failures, and retries.
- Track active and unhealthy workers.
- Detect jobs exceeding their execution deadlines.
- Support operational dashboards and alerts.

Metrics must not expose passwords, tokens, or sensitive job payloads.

---

## 5. Job Lifecycle

Every job follows a defined lifecycle.

```text
                    +-------------+
                    |   CREATED   |
                    +-------------+
                           |
                           v
                    +-------------+
                    |   PENDING   |
                    +-------------+
                           |
                           v
                    +-------------+
                    |   QUEUED    |
                    +-------------+
                           |
                           v
                    +-------------+
                    |   RUNNING   |
                    +-------------+
                       /       \
                      /         \
                     v           v
              +-----------+  +-----------+
              | SUCCEEDED |  |  FAILED   |
              +-----------+  +-----------+
                                  |
                                  v
                            Retry Eligible?
                              /       \
                            Yes        No
                             |          |
                             v          v
                       +-----------+  +-------------+
                       | RETRY_WAIT|  | DEAD_LETTER|
                       +-----------+  +-------------+
                             |
                             v
                         +--------+
                         | QUEUED |
                         +--------+
```

Cancellation is a separate control path. A cancellation request may transition a pending job directly to `CANCELLED`; for a running job, cancellation is cooperative and depends on handler support.

### Job States

| State | Description |
|---|---|
| `PENDING` | Persisted but not yet dispatched |
| `QUEUED` | Eligible and published for execution |
| `RUNNING` | Claimed by a worker |
| `RETRY_WAIT` | Failed or interrupted; awaiting retry eligibility |
| `SUCCEEDED` | Completed successfully |
| `FAILED` | Terminal failure |
| `DEAD_LETTER` | Exhausted retries and retained for investigation |
| `CANCELLED` | Cancellation completed before or during execution, where supported |

State transitions must be validated. For example, a terminal job should not return to `RUNNING` without an explicit administrative or retry operation.

The database should record state changes transactionally with relevant execution metadata.

---

## 6. Job Submission and Execution Flow

### Step 1: Submit a job

A client sends a request to:

`POST /api/v1/jobs`

The API authenticates the client, validates the job type and payload, and checks the idempotency key.

### Step 2: Persist the job

The API opens a database transaction and:

1. Creates the job record.
2. Stores the initial state.
3. Creates a transactional outbox event.
4. Commits the transaction.

This ensures that the job and its dispatch intent are persisted together.

### Step 3: Publish the dispatch event

An outbox dispatcher reads unpublished events from PostgreSQL and publishes the corresponding job identifiers to Redis.

After publication, it records that the event was dispatched.

If publication succeeds but recording the dispatch fails, the event may be published again. Workers must therefore tolerate duplicate deliveries.

### Step 4: Claim the job

A worker receives a job identifier and attempts to claim the job.

The claim operation verifies that:

- The job exists.
- The job is eligible for execution.
- The job is not cancelled.
- The job has not exceeded its retry limit.
- Another active worker has not already claimed it.

The claim should be protected using a database transaction, row locking, or another correctly implemented atomic claim mechanism.

### Step 5: Execute the job

The worker loads the registered handler and executes the task.

Examples include:

- Sending a notification.
- Processing a file.
- Generating a report.
- Running a data transformation.

Untrusted clients must not be allowed to submit arbitrary Python code for execution.

### Step 6: Record the outcome

On success, the worker stores the result and transitions the job to `SUCCEEDED`.

On failure, the worker records the error category and attempt details, then determines whether the job should be retried or marked terminal.

### Step 7: Acknowledge the message

The queue message is acknowledged only after the relevant execution outcome has been persisted.

This ordering reduces the risk of losing work, but it does not eliminate duplicates. A worker can finish an external side effect and crash before persisting success or acknowledging the message.

For that reason, job handlers must be idempotent whenever duplicate execution could cause harm.

---

## 7. Database Schema

The initial database design consists of four primary tables.

### 7.1 `jobs`

Stores the current state and configuration of each job.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `tenant_id` | UUID | Owning tenant |
| `job_type` | VARCHAR | Registered handler name |
| `payload` | JSONB | Validated job input |
| `status` | VARCHAR | Current lifecycle state |
| `priority` | INTEGER | Scheduling priority |
| `max_attempts` | INTEGER | Maximum execution attempts |
| `attempt_count` | INTEGER | Number of claimed attempts |
| `scheduled_at` | TIMESTAMPTZ | Earliest execution time |
| `created_at` | TIMESTAMPTZ | Creation timestamp |
| `updated_at` | TIMESTAMPTZ | Last update timestamp |
| `started_at` | TIMESTAMPTZ | Current or most recent start |
| `completed_at` | TIMESTAMPTZ | Terminal completion time |
| `result` | JSONB | Successful result |
| `last_error` | JSONB | Most recent failure summary |
| `idempotency_key` | VARCHAR | Optional client-provided key |

Recommended constraints and indexes:

- Primary key on `id`.
- Index on `(tenant_id, created_at DESC)`.
- Index on `(status, scheduled_at)`.
- Unique constraint on `(tenant_id, idempotency_key)` when the key is not null.
- Check constraints for valid priority and attempt counts.

### 7.2 `job_attempts`

Stores an execution record for every attempt.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `job_id` | UUID | Associated job |
| `attempt_number` | INTEGER | Attempt sequence |
| `worker_id` | VARCHAR | Worker that claimed the job |
| `status` | VARCHAR | Attempt outcome |
| `started_at` | TIMESTAMPTZ | Execution start |
| `finished_at` | TIMESTAMPTZ | Execution finish |
| `error_type` | VARCHAR | Error classification |
| `error_message` | TEXT | Sanitized error summary |
| `duration_ms` | BIGINT | Execution duration |

Recommended constraint:

- Unique constraint on `(job_id, attempt_number)`.

### 7.3 `outbox_events`

Stores events that must be published after a database transaction commits.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Event identifier |
| `job_id` | UUID | Associated job |
| `event_type` | VARCHAR | Event category |
| `payload` | JSONB | Event metadata |
| `created_at` | TIMESTAMPTZ | Creation timestamp |
| `published_at` | TIMESTAMPTZ | Publication confirmation |
| `publish_attempts` | INTEGER | Number of publication attempts |

Unpublished events must be retried. Outbox cleanup should retain sufficient history for diagnosis and operational recovery.

### 7.4 `idempotency_records`

An optional dedicated table can manage request deduplication independently of job lifecycle.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `tenant_id` | UUID | Requesting tenant |
| `idempotency_key` | VARCHAR | Client-supplied key |
| `request_hash` | VARCHAR | Hash of normalized request content |
| `job_id` | UUID | Associated job |
| `created_at` | TIMESTAMPTZ | Record creation timestamp |
| `expires_at` | TIMESTAMPTZ | Optional retention deadline |

The unique constraint on `(tenant_id, idempotency_key)` prevents concurrent duplicate submissions. Reusing a key with a different request body should return a conflict rather than silently changing the original job.

If a dedicated idempotency table is used, define one authoritative deduplication mechanism instead of redundantly enforcing the same policy in multiple places.

---

## 8. API Design

All endpoints use the `/api/v1` prefix.

### Job Operations

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/jobs` | Submit a job |
| `GET` | `/jobs/{job_id}` | Retrieve job details |
| `GET` | `/jobs` | List jobs with pagination and filters |
| `POST` | `/jobs/{job_id}/retry` | Request a retry for an eligible job |
| `POST` | `/jobs/{job_id}/cancel` | Request cancellation |
| `GET` | `/jobs/{job_id}/attempts` | Retrieve execution history |
| `GET` | `/jobs/{job_id}/result` | Retrieve a successful result |

### Operational Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health/live` | Liveness check |
| `GET` | `/health/ready` | Readiness check |
| `GET` | `/queues/stats` | Queue metrics |
| `GET` | `/workers` | Worker health information |
| `GET` | `/metrics` | Prometheus metrics, appropriately protected |

### Job Submission Example

Request:

```http
POST /api/v1/jobs
Authorization: Bearer <token>
Idempotency-Key: report-2026-001
Content-Type: application/json
```

```json
{
  "job_type": "generate_report",
  "payload": {
    "report_id": "report-123",
    "format": "csv"
  },
  "priority": 5,
  "max_attempts": 3,
  "scheduled_at": null
}
```

Response:

```json
{
  "job_id": "6fd3f5b7-9f4b-4f42-b20d-ae0b7b6c9b22",
  "status": "PENDING",
  "created_at": "2026-10-26T10:00:00Z"
}
```

The API returns a successful creation response after the job and outbox event have been committed. The job may still be pending dispatch when the response is returned.

A repeated request with the same idempotency key and equivalent payload should return the existing job. A conflicting payload should be rejected.

### Job Status Example

```json
{
  "job_id": "6fd3f5b7-9f4b-4f42-b20d-ae0b7b6c9b22",
  "job_type": "generate_report",
  "status": "SUCCEEDED",
  "attempt_count": 1,
  "created_at": "2026-10-26T10:00:00Z",
  "started_at": "2026-10-26T10:00:02Z",
  "completed_at": "2026-10-26T10:00:05Z",
  "result": {
    "file_name": "report.csv",
    "records_processed": 1500
  }
}
```

The timestamps and values above are illustrative.

---

## 9. Retry and Failure Handling

Transient failures should not immediately cause permanent job failure.

### Retry Policy

Use exponential backoff with jitter.

A conceptual delay formula is:

`delay = min(max_delay, base_delay × 2^(attempt - 1)) + jitter`

For example, a policy might use a one-second base delay, a maximum delay of five minutes, and randomized jitter. These are configurable values, not fixed system requirements.

### Retryable Failures

Examples include:

- Temporary network failures.
- Rate limiting from an external service.
- Transient database connectivity problems.
- Temporary service unavailability.

### Non-Retryable Failures

Examples include:

- Invalid job payloads.
- Unsupported job types.
- Permanent validation errors.
- Authorization failures that will not be resolved by retrying.

### Dead-Letter Handling

A job becomes eligible for dead-letter handling when it exhausts its permitted attempts or encounters a designated terminal failure.

Dead-letter records should retain:

- Job identifier.
- Attempt history.
- Failure classification.
- Sanitized error details.
- Timestamps.
- Retry policy information.

Administrative retry operations must be authenticated, authorized, audited, and subject to the same idempotency and concurrency protections as normal execution.

---

## 10. Reliability and Concurrency

### 10.1 At-Least-Once Delivery

TaskFlow aims to ensure that eligible work is retried or recovered after failures.

Duplicate delivery is possible. Therefore, the system does not promise exactly-once execution for arbitrary jobs or external side effects.

### 10.2 Idempotency

Job handlers should produce the same logical outcome when the same operation is retried.

For example, a payment-related handler should use an external provider's idempotency mechanism rather than assuming that a queue message will be delivered only once.

### 10.3 Atomic Job Claiming

Only one active claim should be valid for a given execution attempt.

Use PostgreSQL transactions and row-level locking or an equivalent atomic claim design. Redis locks alone should not be treated as proof that an old worker can no longer modify the job.

### 10.4 Leases and Heartbeats

Long-running jobs can maintain a lease or heartbeat.

A recovery process detects expired leases and determines whether the job should be retried. Lease expiry does not prove that the original worker has stopped, so the system should use attempt identifiers or fencing tokens where stale workers could otherwise overwrite newer results.

### 10.5 Transactional Outbox

The outbox pattern closes the gap between committing a job record and publishing its dispatch event.

It does not make PostgreSQL and Redis one atomic system. Publication can still be repeated, so consumers must remain idempotent.

### 10.6 Graceful Shutdown

On shutdown, a worker should:

1. Stop claiming new jobs.
2. Finish current work within a configured grace period when possible.
3. Persist the outcome.
4. Release or expire the execution lease safely.
5. Leave unfinished work eligible for recovery.

---

## 11. Security

Security requirements include:

- Authentication for all private endpoints.
- Tenant-level authorization for job access.
- Secure password hashing when passwords are used.
- Short-lived access tokens and controlled refresh mechanisms where applicable.
- Rate limiting on submission and retry endpoints.
- Request-size limits and strict payload validation.
- Allowlisted job types.
- No arbitrary Python execution from client-supplied code.
- Secrets supplied through environment configuration or a secrets manager.
- TLS for production network traffic.
- Restricted database and Redis network access.
- Sanitized errors that do not expose credentials or internal stack traces.
- Audit records for sensitive administrative actions.
- Protection against log injection and sensitive-data leakage.

The `/metrics` endpoint should not expose sensitive operational information publicly.

---

## 12. Observability

### Core Metrics

| Metric | Purpose |
|---|---|
| `taskflow_jobs_submitted_total` | Total submitted jobs |
| `taskflow_jobs_completed_total` | Completed jobs by outcome |
| `taskflow_job_duration_seconds` | Execution duration distribution |
| `taskflow_queue_depth` | Number of waiting jobs |
| `taskflow_queue_wait_seconds` | Time from eligibility to execution |
| `taskflow_job_retries_total` | Retry frequency |
| `taskflow_worker_active_jobs` | Current active work |
| `taskflow_worker_heartbeat_age_seconds` | Worker liveness |
| `taskflow_outbox_pending_events` | Undispatched event backlog |

Metric labels must have bounded cardinality. Avoid using individual job IDs or other unbounded identifiers as Prometheus labels.

### Suggested Alerts

- Queue depth remains above a configured threshold.
- Oldest eligible job exceeds its waiting-time threshold.
- Failure rate rises above an acceptable level.
- No healthy workers are available.
- Outbox publication falls behind.
- PostgreSQL or Redis connectivity is lost.
- Jobs repeatedly expire without completing.

Thresholds should be established from measured behavior rather than chosen arbitrarily.

---

## 13. Performance and Scalability

The system should be benchmarked under controlled workloads.

### Performance Goals

Measure:

- API submission throughput.
- API p50, p95, and p99 latency.
- Queue waiting time.
- Job execution throughput.
- Retry and failure rates.
- Database query latency.
- Worker utilization.
- Recovery time after process failures.

### Scaling Strategy

**API layer**

Run multiple stateless API replicas behind a load balancer.

**Worker layer**

Increase worker replicas independently of API replicas. Use bounded concurrency so workers do not exhaust database connections or overwhelm downstream services.

**Database layer**

Add appropriate indexes, use connection pooling, inspect query plans, and archive old execution history when retention requirements demand it.

**Redis layer**

Monitor queue depth, memory usage, persistence settings, and latency. Select deployment and recovery settings based on the required durability guarantees.

**Priority queues**

Separate queues can support priority classes, but the dispatch strategy must prevent starvation of lower-priority jobs.

### Benchmarking Method

Use Locust or k6 to generate a repeatable workload.

Record:

- Machine specifications.
- Worker count.
- Job type and duration.
- Payload size.
- Submission rate.
- Concurrency.
- Completion throughput.
- Latency percentiles.
- Failure rate.

Report measured results rather than claiming a predetermined throughput before testing.

---

## 14. Project Directory Structure

```text
taskflow/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── dependencies.py
│   │   └── v1/
│   │       ├── jobs.py
│   │       ├── workers.py
│   │       └── health.py
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   └── logging.py
│   ├── db/
│   │   ├── session.py
│   │   ├── base.py
│   │   └── models/
│   │       ├── job.py
│   │       ├── job_attempt.py
│   │       ├── outbox_event.py
│   │       └── idempotency_record.py
│   ├── schemas/
│   │   ├── job.py
│   │   └── common.py
│   ├── services/
│   │   ├── job_service.py
│   │   ├── dispatch_service.py
│   │   ├── retry_service.py
│   │   └── recovery_service.py
│   ├── queue/
│   │   ├── redis_client.py
│   │   └── publisher.py
│   ├── workers/
│   │   ├── runner.py
│   │   ├── registry.py
│   │   └── handlers/
│   │       ├── generate_report.py
│   │       ├── process_file.py
│   │       └── send_notification.py
│   ├── scheduler/
│   │   └── scheduler.py
│   └── monitoring/
│       └── metrics.py
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── reliability/
│   └── load/
├── scripts/
│   ├── create_test_jobs.py
│   └── benchmark.py
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
├── docker/
│   └── worker.Dockerfile
├── .env.example
├── .gitignore
├── alembic.ini
├── compose.yaml
├── Dockerfile
├── pyproject.toml
└── README.md
```

This structure is a target architecture. Implement modules incrementally; do not create empty files for every component before the core workflow works.

---

## 15. Local Development Setup

### Prerequisites

- Python 3.12 or another supported Python version.
- Docker and Docker Compose.
- Git.
- A code editor.

### Setup

Clone the repository:

```bash
git clone <your-repository-url>
cd taskflow
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

Install dependencies using the project's configured package manager. For a pip-based setup:

```bash
pip install -e ".[dev]"
```

Create your environment configuration:

```bash
cp .env.example .env
```

On Windows Command Prompt, use:

```cmd
copy .env.example .env
```

Configure the database URL, Redis connection, authentication settings, and other required environment variables. Do not commit secrets.

Start the infrastructure:

```bash
docker compose up -d postgres redis
```

Run database migrations:

```bash
alembic upgrade head
```

Start the API:

```bash
uvicorn app.main:app --reload
```

Start the worker in a separate terminal:

```bash
python -m app.workers.runner
```

The exact commands may change as the implementation evolves.

---

## 16. Testing Strategy

### Unit Tests

Test:

- Job validation.
- State-transition rules.
- Retry delay calculations.
- Idempotency behavior.
- Failure classification.
- Authorization logic.

### Integration Tests

Test:

- Job submission and persistence.
- Outbox publication.
- Redis dispatch.
- Worker execution.
- Result persistence.
- Retry scheduling.
- Database transaction rollback.

### Reliability Tests

Simulate:

- Worker termination during execution.
- Redis unavailability.
- PostgreSQL connection loss.
- Duplicate queue messages.
- Repeated dispatch events.
- Expired worker leases.
- Concurrent retry requests.
- Duplicate submission requests.
- Graceful shutdown during active work.

### Load Tests

Measure API throughput, queue wait time, worker throughput, and latency under increasing concurrency.

Every reliability fix should have a regression test.

---

## 17. Deployment Architecture

A production deployment can use the following logical arrangement:

```text
                    Internet / Clients
                           |
                           v
                    HTTPS Load Balancer
                           |
                           v
                    FastAPI Replicas
                           |
                +----------+----------+
                |                     |
                v                     v
          PostgreSQL                Redis
                ^                     ^
                |                     |
                |               Queue Dispatch
                |                     |
                +-----------+---------+
                            |
                            v
                    Background Workers

                  Monitoring and Alerts
                    /             \
                   v               v
              Prometheus         Grafana
```

For a first deployment, Docker Compose is sufficient for local development. A small cloud VM or managed container service can be used for a portfolio deployment.

Production deployment should additionally consider:

- Automated database backups.
- Database migration procedures.
- TLS and secret management.
- Resource limits.
- Worker concurrency limits.
- Health checks.
- Restart policies.
- Centralized logs.
- Monitoring and alerting.
- Disaster recovery and restoration testing.

Do not introduce Kubernetes until there is a concrete need for its orchestration capabilities.

---

## 18. Implementation Roadmap

### Phase 1: Core Job API
- [ ] Initialize the FastAPI project.
- [ ] Configure PostgreSQL and SQLAlchemy.
- [ ] Create the job schema and migrations.
- [ ] Implement job submission and retrieval.
- [ ] Add pagination and filtering.
- [ ] Write unit and integration tests.

### Phase 2: Asynchronous Execution
- [ ] Add Redis.
- [ ] Implement the worker runner.
- [ ] Create a handler registry.
- [ ] Execute submitted jobs asynchronously.
- [ ] Persist job attempts and results.
- [ ] Add basic worker health checks.

### Phase 3: Reliable Dispatch
- [ ] Implement the transactional outbox.
- [ ] Add a publisher process.
- [ ] Handle duplicate messages.
- [ ] Implement atomic job claiming.
- [ ] Add execution leases and recovery.
- [ ] Test Redis and worker failure scenarios.

### Phase 4: Advanced Job Management
- [ ] Implement retries with exponential backoff and jitter.
- [ ] Add dead-letter handling.
- [ ] Add idempotency keys.
- [ ] Implement job priorities.
- [ ] Add scheduling.
- [ ] Implement cooperative cancellation.

### Phase 5: Production Engineering
- [ ] Add authentication and authorization.
- [ ] Add request rate limits.
- [ ] Add structured logging.
- [ ] Export Prometheus metrics.
- [ ] Build Grafana dashboards.
- [ ] Add Docker Compose.
- [ ] Configure CI/CD.
- [ ] Perform load and reliability testing.
- [ ] Document measured performance results.

---

## 19. Engineering Trade-offs

### Why PostgreSQL and Redis?

PostgreSQL provides durable records, transactions, constraints, and queryable execution history. Redis provides fast queueing and coordination.

Their responsibilities are distinct, and the transactional outbox handles the gap between database commits and queue publication.

### Why independent workers?

Independent workers allow the system to increase execution capacity without increasing the number of API processes. They also isolate long-running tasks from HTTP request handling.

### Why at-least-once delivery?

Reliable recovery can require redelivery after uncertain failures. At-least-once delivery is a practical model, provided that duplicate execution is expected and handlers are designed accordingly.

### Why not promise exactly-once execution?

A worker may perform an external side effect and crash before recording success. Without cooperation from the downstream system or a shared transaction, the platform cannot reliably distinguish every completed side effect from an incomplete one.

### Why not use microservices initially?

A modular application with independent API, dispatcher, scheduler, and worker processes provides meaningful separation without introducing unnecessary network boundaries and operational complexity.

---

## 20. Success Criteria

The initial project is complete when:

- A client can submit a job through the API.
- The job is persisted before dispatch.
- A background worker can execute the job independently.
- Job status and results can be retrieved through the API.
- Failed jobs can be retried according to policy.
- Duplicate submission requests are handled safely.
- Duplicate queue delivery does not corrupt job state.
- Unpublished outbox events are recovered.
- Worker crashes do not permanently strand eligible jobs.
- Authentication and tenant authorization are tested.
- Automated unit and integration tests pass.
- The system can be started locally using documented commands.
- Performance and recovery behavior are measured and documented.

---

## 21. Future Enhancements

Potential extensions include:

- Multi-tenant quotas.
- Recurring jobs.
- Workflow orchestration with task dependencies.
- Event-driven job submission.
- WebSocket-based status updates.
- A React monitoring dashboard.
- Worker autoscaling based on queue depth.
- Per-tenant priority policies.
- Job artifact storage using object storage.
- OpenTelemetry distributed tracing.
- Multi-node deployment and disaster recovery.

These features should be added only after the core execution and recovery workflow is reliable.

---

## 22. Learning Outcomes

Building TaskFlow provides practical experience with:

- REST API design.
- PostgreSQL schema design and transaction management.
- Redis-backed asynchronous processing.
- Distributed worker coordination.
- Idempotency and duplicate delivery.
- Retry policies and backoff.
- Concurrency control.
- Failure recovery.
- Observability and performance testing.
- Containerized deployment.
- Automated testing and CI/CD.

The goal is not simply to process background tasks. It is to understand and demonstrate how a backend system behaves under concurrency, failures, retries, and increasing workload.

---

**TaskFlow — Reliable background execution through durable state, distributed workers, and observable processing.**