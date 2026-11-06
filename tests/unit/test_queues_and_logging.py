import random
from collections import Counter

from core.logging import sanitize
from taskqueue import queues


def test_priority_mapping():
    assert queues.queue_for_priority(9) == queues.QUEUE_HIGH
    assert queues.queue_for_priority(7) == queues.QUEUE_HIGH
    assert queues.queue_for_priority(5) == queues.QUEUE_NORMAL
    assert queues.queue_for_priority(0) == queues.QUEUE_LOW


def test_weighted_order_prefers_high_but_never_starves_low():
    rng = random.Random(7)
    firsts = Counter(queues.weighted_queue_order((6, 3, 1), rng)[0] for _ in range(5000))
    assert firsts[queues.QUEUE_HIGH] > firsts[queues.QUEUE_NORMAL] > firsts[queues.QUEUE_LOW] > 0


def test_weighted_order_contains_every_queue_once():
    order = queues.weighted_queue_order((6, 3, 1))
    assert sorted(order) == sorted(queues.QUEUES)


def test_fetch_moves_to_processing_and_ack_removes(redis_client):
    queues.publish(redis_client, "job-1", 5)
    assert queues.fetch(redis_client, "w1", (6, 3, 1)) == "job-1"
    processing = queues.PROCESSING_KEY.format(worker_id="w1")
    assert redis_client.lrange(processing, 0, -1) == ["job-1"]
    queues.ack(redis_client, "w1", "job-1")
    assert redis_client.llen(processing) == 0
    assert queues.fetch(redis_client, "w1", (6, 3, 1)) is None


def test_fifo_within_a_queue(redis_client):
    for job_id in ("a", "b", "c"):
        queues.publish(redis_client, job_id, 5)
    assert [queues.fetch(redis_client, "w", (6, 3, 1)) for _ in range(3)] == ["a", "b", "c"]


def test_sanitize_redacts_credentials():
    text = sanitize("Authorization: Bearer abc.def.ghi password=hunter2 postgresql://u:secret@db/x")
    assert "abc.def.ghi" not in text
    assert "hunter2" not in text
    assert "secret" not in text


def test_sanitize_prevents_log_injection():
    assert "\n" not in sanitize("ok\nFAKE LOG LINE")


def test_sanitize_truncates():
    assert len(sanitize("x" * 5000, max_length=100)) < 150
