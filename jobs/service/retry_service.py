import random

from jobs.state import JobStatus


def compute_backoff(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: float,
    rng: random.Random | None = None,
) -> float:
    """delay = min(max_delay, base_delay * 2^(attempt - 1)) + jitter"""
    rng = rng or random
    exponential = base_delay * (2 ** max(attempt - 1, 0))
    return min(max_delay, exponential) + rng.uniform(0, jitter)


def outcome_after_failure(retryable: bool, attempt_number: int, max_attempts: int) -> JobStatus:
    if not retryable:
        return JobStatus.FAILED
    if attempt_number >= max_attempts:
        return JobStatus.DEAD_LETTER
    return JobStatus.RETRY_WAIT
