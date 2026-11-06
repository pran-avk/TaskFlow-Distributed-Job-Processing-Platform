import random

import pytest

from jobs.service.retry_service import compute_backoff, outcome_after_failure
from jobs.state import JobStatus


def test_backoff_doubles_without_jitter():
    delays = [compute_backoff(n, base_delay=1, max_delay=300, jitter=0) for n in range(1, 6)]
    assert delays == [1, 2, 4, 8, 16]


def test_backoff_is_capped():
    assert compute_backoff(20, base_delay=1, max_delay=300, jitter=0) == 300


def test_jitter_stays_within_bounds():
    rng = random.Random(42)
    for attempt in range(1, 10):
        delay = compute_backoff(attempt, base_delay=1, max_delay=60, jitter=0.5, rng=rng)
        base = min(60, 2 ** (attempt - 1))
        assert base <= delay <= base + 0.5


def test_jitter_spreads_retries():
    rng = random.Random(1)
    delays = {compute_backoff(3, 1, 300, 1.0, rng) for _ in range(20)}
    assert len(delays) > 1


@pytest.mark.parametrize("retryable,attempt,max_attempts,expected", [
    (True, 1, 3, JobStatus.RETRY_WAIT),
    (True, 2, 3, JobStatus.RETRY_WAIT),
    (True, 3, 3, JobStatus.DEAD_LETTER),
    (False, 1, 3, JobStatus.FAILED),
    (False, 3, 3, JobStatus.FAILED),
    (True, 1, 1, JobStatus.DEAD_LETTER),
])
def test_outcome_after_failure(retryable, attempt, max_attempts, expected):
    assert outcome_after_failure(retryable, attempt, max_attempts) == expected
