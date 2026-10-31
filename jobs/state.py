from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.DEAD_LETTER,
    JobStatus.CANCELLED,
})

# States a job may be dispatched from (outbox -> Redis).
DISPATCHABLE_STATES = frozenset({JobStatus.PENDING, JobStatus.RETRY_WAIT})

# States that can be cancelled without worker cooperation.
DIRECTLY_CANCELLABLE_STATES = frozenset({JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RETRY_WAIT})

# States from which a client may request a manual retry.
MANUALLY_RETRYABLE_STATES = frozenset({JobStatus.FAILED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED})

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED,
        JobStatus.RETRY_WAIT,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTER,
        JobStatus.CANCELLED,
    }),
    JobStatus.RETRY_WAIT: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    # Terminal failures only leave via an explicit (audited) manual retry.
    JobStatus.FAILED: frozenset({JobStatus.PENDING}),
    JobStatus.DEAD_LETTER: frozenset({JobStatus.PENDING}),
    JobStatus.CANCELLED: frozenset({JobStatus.PENDING}),
}


def can_transition(current: JobStatus | str, target: JobStatus | str) -> bool:
    return JobStatus(target) in ALLOWED_TRANSITIONS[JobStatus(current)]


def sources_for(target: JobStatus) -> list[str]:
    """All states that may legally move to `target` (for conditional UPDATE ... WHERE status IN)."""
    return [str(source) for source, targets in ALLOWED_TRANSITIONS.items() if target in targets]
