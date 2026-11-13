"""Prometheus metrics. Labels are restricted to bounded sets (job type from the
handler allowlist, outcome, queue name, worker id) — never job ids or payloads."""
import logging
import time

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

JOBS_SUBMITTED = Counter(
    "taskflow_jobs_submitted_total", "Total submitted jobs", ["job_type"]
)
JOBS_COMPLETED = Counter(
    "taskflow_jobs_completed_total", "Jobs reaching an attempt outcome", ["job_type", "outcome"]
)
JOB_DURATION = Histogram(
    "taskflow_job_duration_seconds", "Handler execution duration", ["job_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
QUEUE_WAIT = Histogram(
    "taskflow_queue_wait_seconds", "Time from eligibility to execution start", ["job_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)
JOB_RETRIES = Counter(
    "taskflow_job_retries_total", "Retries scheduled after a failed attempt", ["job_type"]
)
WORKER_ACTIVE_JOBS = Gauge(
    "taskflow_worker_active_jobs", "Jobs currently executing in this worker process"
)
OUTBOX_PUBLISHED = Counter(
    "taskflow_outbox_published_total", "Outbox events published to Redis"
)
RECOVERED = Counter(
    "taskflow_recovered_total", "Recovery actions taken by the scheduler", ["kind"]
)


class QueueStateCollector(Collector):
    """Scrape-time gauges read from Redis and PostgreSQL (registered by the API process)."""

    def __init__(self, session_factory, redis_getter, heartbeat_ttl_seconds: float):
        self._session_factory = session_factory
        self._redis_getter = redis_getter
        self._heartbeat_ttl = heartbeat_ttl_seconds

    def describe(self):
        # Lets the registry learn metric names without querying Redis/PostgreSQL at startup.
        yield GaugeMetricFamily("taskflow_queue_depth", "Jobs waiting in Redis", labels=["queue"])
        yield GaugeMetricFamily("taskflow_outbox_pending_events", "Unpublished outbox events")
        yield GaugeMetricFamily(
            "taskflow_worker_heartbeat_age_seconds", "Seconds since last worker heartbeat", labels=["worker_id"]
        )
        yield GaugeMetricFamily("taskflow_jobs", "Jobs currently in each lifecycle stage", labels=["status"])

    def collect(self):
        from jobs.models import Job, OutboxEvent
        from jobs.state import JobStatus
        from taskqueue import queues

        depth = GaugeMetricFamily("taskflow_queue_depth", "Jobs waiting in Redis", labels=["queue"])
        outbox = GaugeMetricFamily("taskflow_outbox_pending_events", "Unpublished outbox events")
        heartbeat = GaugeMetricFamily(
            "taskflow_worker_heartbeat_age_seconds", "Seconds since last worker heartbeat", labels=["worker_id"]
        )
        stages = GaugeMetricFamily("taskflow_jobs", "Jobs currently in each lifecycle stage", labels=["status"])
        try:
            client = self._redis_getter()
            for name, size in queues.queue_depths(client).items():
                depth.add_metric([name], size)
            now = time.time()
            for worker_id in client.smembers(queues.WORKERS_SET):
                last = client.hget(queues.WORKER_KEY.format(worker_id=worker_id), "last_heartbeat")
                age = now - float(last) if last else self._heartbeat_ttl
                heartbeat.add_metric([worker_id], age)
        except Exception:
            logger.warning("Could not read queue metrics from Redis")
        try:
            with self._session_factory() as db:
                pending = db.scalar(
                    select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
                )
            outbox.add_metric([], pending or 0)
            with self._session_factory() as db:
                counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
            for status in JobStatus:  # every stage, so empty ones report 0 instead of vanishing
                stages.add_metric([status.value], counts.get(status.value, 0))
        except Exception:
            logger.warning("Could not read outbox metrics from database")
        yield depth
        yield outbox
        yield heartbeat
        yield stages
