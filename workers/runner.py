"""Worker process: `python -m workers.runner`

Each of N slot threads repeatedly takes a job id from Redis, claims the job in
PostgreSQL, runs its handler in a separate thread while renewing the lease, records
the outcome (fenced by attempt number) and only then acknowledges the message.
"""
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid

import pydantic
import redis
from prometheus_client import start_http_server
from sqlalchemy.orm import sessionmaker

from core.config import Settings, get_settings
from core.logging import configure_logging, sanitize
from database import SessionLocal
from jobs.service import execution_service
from jobs.service.execution_service import ClaimedJob
from jobs.state import AttemptStatus, JobStatus
from ops.metrics import JOB_DURATION, JOB_RETRIES, JOBS_COMPLETED, QUEUE_WAIT, WORKER_ACTIVE_JOBS
from taskqueue import queues
from taskqueue.redis_client import get_redis
from workers.context import STOP_CANCEL, STOP_LEASE_LOST, STOP_SHUTDOWN, STOP_TIMEOUT, JobContext
from workers.errors import JobCancelled, JobTimeout, PermanentError, classify
from workers.registry import get_handler

logger = logging.getLogger(__name__)

TIMEOUT_GRACE_SECONDS = 2.0


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        redis_client: redis.Redis | None = None,
        settings: Settings | None = None,
        worker_id: str | None = None,
    ):
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.redis = redis_client or get_redis()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.started_at = time.time()
        self._stopping = threading.Event()
        self._force_stop = threading.Event()
        self._active: dict[uuid.UUID, JobContext] = {}
        self._lock = threading.Lock()
        self._processed = 0
        self._failed = 0

    # -- message handling -------------------------------------------------

    def process_one(self) -> bool:
        """Fetch and handle one message. Returns False when the queues are empty."""
        job_id = queues.fetch(self.redis, self.worker_id, self.settings.queue_weights)
        if job_id is None:
            return False
        self.handle_message(job_id)
        return True

    def handle_message(self, raw_job_id: str) -> None:
        try:
            job_id = uuid.UUID(raw_job_id)
        except ValueError:
            logger.error("Discarding malformed queue message")
            queues.ack(self.redis, self.worker_id, raw_job_id)
            return

        try:
            with self.session_factory() as db:
                claim = execution_service.claim_job(
                    db, job_id, self.worker_id, self.settings.worker_lease_seconds
                )
        except Exception:
            # Database unavailable: hand the message back and slow down.
            logger.exception("Claim failed; returning message to queue", extra={"job_id": job_id})
            self._requeue(raw_job_id)
            time.sleep(1.0)
            return

        if claim is None:
            logger.info("Skipping duplicate or ineligible message", extra={"job_id": job_id})
            queues.ack(self.redis, self.worker_id, raw_job_id)
            return

        try:
            self._execute(claim)
        except Exception:
            # Outcome could not be persisted; the lease will expire and recovery retries the job.
            logger.exception("Failed to record outcome", extra={"job_id": job_id})
        finally:
            queues.ack(self.redis, self.worker_id, raw_job_id)

    def _requeue(self, raw_job_id: str) -> None:
        try:
            pipe = self.redis.pipeline()
            pipe.lrem(queues.PROCESSING_KEY.format(worker_id=self.worker_id), 1, raw_job_id)
            pipe.lpush(queues.QUEUE_HIGH, raw_job_id)
            pipe.execute()
        except redis.RedisError:
            logger.exception("Could not requeue message")

    # -- execution --------------------------------------------------------

    def _execute(self, claim: ClaimedJob) -> None:
        log_extra = {"job_id": claim.job_id, "job_type": claim.job_type, "attempt": claim.attempt_number}
        QUEUE_WAIT.labels(job_type=claim.job_type).observe(
            max((claim.started_at - claim.scheduled_at).total_seconds(), 0)
        )
        started = time.monotonic()
        ctx = JobContext(job_id=claim.job_id, tenant_id=claim.tenant_id, attempt_number=claim.attempt_number)

        with self._lock:
            self._active[claim.job_id] = ctx
        WORKER_ACTIVE_JOBS.inc()
        try:
            result, error = self._run_handler(claim, ctx)
        finally:
            WORKER_ACTIVE_JOBS.dec()
            with self._lock:
                self._active.pop(claim.job_id, None)
        duration_ms = int((time.monotonic() - started) * 1000)

        if ctx.stop_reason == STOP_LEASE_LOST:
            logger.warning("Lease lost; discarding outcome", extra=log_extra)
            return

        if error is None:
            try:
                result = self._normalise_result(result)
            except PermanentError as exc:
                error = exc

        with self.session_factory() as db:
            if error is None:
                if execution_service.complete_job(db, claim, result, duration_ms):
                    JOB_DURATION.labels(job_type=claim.job_type).observe(duration_ms / 1000)
                    JOBS_COMPLETED.labels(job_type=claim.job_type, outcome="succeeded").inc()
                    self._processed += 1
                    logger.info("Job succeeded", extra={**log_extra, "status": JobStatus.SUCCEEDED})
                else:
                    logger.warning("Lost ownership before recording success", extra=log_extra)
                return

            if isinstance(error, JobCancelled):
                if execution_service.cancel_running_job(db, claim, duration_ms):
                    JOBS_COMPLETED.labels(job_type=claim.job_type, outcome="cancelled").inc()
                    logger.info("Job cancelled", extra=log_extra)
                return

            error_type, retryable = classify(error)
            attempt_status = AttemptStatus.TIMED_OUT if isinstance(error, JobTimeout) else AttemptStatus.FAILED
            new_status = execution_service.fail_job(
                db, claim, error_type, sanitize(str(error) or error_type, 500), retryable,
                duration_ms, attempt_status,
            )
        if new_status is None:
            logger.warning("Lost ownership before recording failure", extra=log_extra)
            return
        self._failed += 1
        JOB_DURATION.labels(job_type=claim.job_type).observe(duration_ms / 1000)
        JOBS_COMPLETED.labels(job_type=claim.job_type, outcome=new_status.lower()).inc()
        if new_status == JobStatus.RETRY_WAIT:
            JOB_RETRIES.labels(job_type=claim.job_type).inc()
        logger.warning("Job attempt failed: %s", error_type, extra={**log_extra, "status": new_status})

    def _run_handler(self, claim: ClaimedJob, ctx: JobContext) -> tuple[dict | None, BaseException | None]:
        try:
            spec = get_handler(claim.job_type)
            payload = spec.payload_model.model_validate(claim.payload)
        except (PermanentError, pydantic.ValidationError) as exc:
            return None, exc

        outcome: dict = {}

        def target():
            try:
                outcome["result"] = spec.func(payload, ctx)
            except BaseException as exc:  # noqa: BLE001 - reported to the job record
                outcome["error"] = exc

        thread = threading.Thread(target=target, name=f"job-{claim.job_id}", daemon=True)
        thread.start()
        deadline = time.monotonic() + spec.timeout_seconds
        abandon_at = None
        interval = self.settings.worker_heartbeat_interval_seconds

        while thread.is_alive():
            now = time.monotonic()
            wait = min(interval, max(deadline - now, 0.05))
            if abandon_at is not None:
                wait = min(wait, max(abandon_at - now, 0.05))
            thread.join(wait)
            if not thread.is_alive():
                break

            now = time.monotonic()
            if now >= deadline and abandon_at is None:
                ctx.request_stop(STOP_TIMEOUT)
                abandon_at = now + TIMEOUT_GRACE_SECONDS
            if abandon_at is not None and now >= abandon_at:
                # Python threads cannot be killed; the fenced writes make any late result harmless.
                return None, JobTimeout(f"Exceeded {spec.timeout_seconds}s timeout")
            if self._force_stop.is_set():
                ctx.request_stop(STOP_SHUTDOWN)

            try:
                with self.session_factory() as db:
                    still_owner, cancel_requested = execution_service.renew_lease(
                        db, claim, self.settings.worker_lease_seconds
                    )
            except Exception:
                logger.exception("Lease renewal failed", extra={"job_id": claim.job_id})
                continue
            if not still_owner:
                ctx.request_stop(STOP_LEASE_LOST)
                return None, None
            if cancel_requested:
                ctx.request_stop(STOP_CANCEL)

        if "error" in outcome:
            return None, outcome["error"]
        return outcome.get("result"), None

    @staticmethod
    def _normalise_result(result) -> dict:
        if result is None:
            result = {}
        if not isinstance(result, dict):
            result = {"value": result}
        try:
            return json.loads(json.dumps(result))
        except (TypeError, ValueError) as exc:
            raise PermanentError("Handler result is not JSON serialisable") from exc

    # -- lifecycle --------------------------------------------------------

    def heartbeat(self) -> None:
        key = queues.WORKER_KEY.format(worker_id=self.worker_id)
        ttl = max(int(self.settings.worker_heartbeat_interval_seconds * 3), 5)
        with self._lock:
            active = len(self._active)
        pipe = self.redis.pipeline()
        pipe.hset(key, mapping={
            "worker_id": self.worker_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "started_at": self.started_at,
            "last_heartbeat": time.time(),
            "active_jobs": active,
            "concurrency": self.settings.worker_concurrency,
            "processed": self._processed,
            "failed": self._failed,
            "state": "draining" if self._stopping.is_set() else "running",
        })
        pipe.expire(key, ttl)
        pipe.sadd(queues.WORKERS_SET, self.worker_id)
        pipe.execute()

    def _heartbeat_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                self.heartbeat()
            except redis.RedisError:
                logger.warning("Heartbeat failed")
            self._stopped.wait(self.settings.worker_heartbeat_interval_seconds)

    def _slot_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if not self.process_one():
                    self._stopping.wait(self.settings.worker_poll_interval_seconds)
            except redis.RedisError:
                logger.warning("Redis unavailable; backing off")
                self._stopping.wait(2.0)
            except Exception:
                logger.exception("Unexpected worker error")
                self._stopping.wait(1.0)

    def stop(self, *_args) -> None:
        if not self._stopping.is_set():
            logger.info("Shutdown requested; finishing in-flight jobs", extra={"worker_id": self.worker_id})
        self._stopping.set()

    def run(self) -> None:
        self._stopped = threading.Event()
        if self.settings.metrics_port:
            try:
                start_http_server(self.settings.metrics_port)
            except OSError:
                logger.warning("Metrics port %s unavailable", self.settings.metrics_port)

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)

        heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        heartbeat.start()
        slots = [
            threading.Thread(target=self._slot_loop, name=f"slot-{i}", daemon=True)
            for i in range(self.settings.worker_concurrency)
        ]
        for slot in slots:
            slot.start()
        logger.info("Worker started with %d slots", len(slots), extra={"worker_id": self.worker_id})

        while not self._stopping.is_set():
            self._stopping.wait(1.0)

        # 1. Stop claiming (slots exit after their current job). 2. Wait for the grace period.
        grace_end = time.monotonic() + self.settings.worker_shutdown_grace_seconds
        for slot in slots:
            slot.join(max(grace_end - time.monotonic(), 0))
        # 3. Interrupt whatever is still running so it records a retryable outcome.
        if any(slot.is_alive() for slot in slots):
            self._force_stop.set()
            with self._lock:
                for ctx in self._active.values():
                    ctx.request_stop(STOP_SHUTDOWN)
            for slot in slots:
                slot.join(self.settings.worker_heartbeat_interval_seconds + TIMEOUT_GRACE_SECONDS)

        self._stopped.set()
        self._deregister()
        logger.info("Worker stopped", extra={"worker_id": self.worker_id})

    def _deregister(self) -> None:
        try:
            self.redis.delete(queues.WORKER_KEY.format(worker_id=self.worker_id))
            # Keep the worker listed if messages remain so recovery can return them to the queue.
            if self.redis.llen(queues.PROCESSING_KEY.format(worker_id=self.worker_id)) == 0:
                self.redis.srem(queues.WORKERS_SET, self.worker_id)
        except redis.RedisError:
            logger.warning("Could not deregister worker")


def main() -> None:
    configure_logging()
    Worker().run()


if __name__ == "__main__":
    main()
