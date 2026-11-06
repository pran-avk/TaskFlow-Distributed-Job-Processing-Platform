import pytest

from jobs.state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    JobStatus,
    can_transition,
    sources_for,
)


@pytest.mark.parametrize("source,target", [
    (JobStatus.PENDING, JobStatus.QUEUED),
    (JobStatus.QUEUED, JobStatus.RUNNING),
    (JobStatus.RUNNING, JobStatus.SUCCEEDED),
    (JobStatus.RUNNING, JobStatus.RETRY_WAIT),
    (JobStatus.RUNNING, JobStatus.DEAD_LETTER),
    (JobStatus.RETRY_WAIT, JobStatus.QUEUED),
    (JobStatus.PENDING, JobStatus.CANCELLED),
    (JobStatus.DEAD_LETTER, JobStatus.PENDING),
])
def test_allowed_transitions(source, target):
    assert can_transition(source, target)


@pytest.mark.parametrize("source,target", [
    (JobStatus.SUCCEEDED, JobStatus.RUNNING),
    (JobStatus.FAILED, JobStatus.RUNNING),
    (JobStatus.PENDING, JobStatus.RUNNING),
    (JobStatus.QUEUED, JobStatus.SUCCEEDED),
    (JobStatus.CANCELLED, JobStatus.QUEUED),
    (JobStatus.SUCCEEDED, JobStatus.PENDING),
])
def test_rejected_transitions(source, target):
    assert not can_transition(source, target)


def test_terminal_states_never_return_to_running():
    for state in TERMINAL_STATES:
        assert JobStatus.RUNNING not in ALLOWED_TRANSITIONS[state]


def test_succeeded_is_final():
    assert ALLOWED_TRANSITIONS[JobStatus.SUCCEEDED] == frozenset()


def test_sources_for_running_is_only_queued():
    assert sources_for(JobStatus.RUNNING) == [JobStatus.QUEUED]
