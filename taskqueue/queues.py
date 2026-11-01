"""Redis key layout and queue primitives.

Queue entries are job ids only; PostgreSQL holds the authoritative job record.
Workers move entries into a per-worker processing list (LMOVE) and remove them
only after the outcome is persisted, so a crashed worker's messages can be
returned to the queue by the recovery service.
"""
import random

import redis

QUEUE_HIGH = "taskflow:queue:high"
QUEUE_NORMAL = "taskflow:queue:normal"
QUEUE_LOW = "taskflow:queue:low"
QUEUES = (QUEUE_HIGH, QUEUE_NORMAL, QUEUE_LOW)

WORKERS_SET = "taskflow:workers"
WORKER_KEY = "taskflow:worker:{worker_id}"
PROCESSING_KEY = "taskflow:processing:{worker_id}"

MIN_PRIORITY = 0
MAX_PRIORITY = 9


def queue_for_priority(priority: int) -> str:
    if priority >= 7:
        return QUEUE_HIGH
    if priority >= 4:
        return QUEUE_NORMAL
    return QUEUE_LOW


def publish(client: redis.Redis, job_id: str, priority: int) -> None:
    client.lpush(queue_for_priority(priority), job_id)


def weighted_queue_order(weights: tuple[int, int, int], rng: random.Random | None = None) -> list[str]:
    """Order queues by weighted random choice so low priority work is never starved."""
    rng = rng or random
    remaining = list(zip(QUEUES, weights))
    order = []
    while remaining:
        total = sum(w for _, w in remaining)
        pick = rng.uniform(0, total)
        upto = 0.0
        for index, (queue, weight) in enumerate(remaining):
            upto += weight
            if pick <= upto:
                order.append(queue)
                remaining.pop(index)
                break
        else:
            order.append(remaining.pop()[0])
    return order


def fetch(client: redis.Redis, worker_id: str, weights: tuple[int, int, int]) -> str | None:
    processing = PROCESSING_KEY.format(worker_id=worker_id)
    for queue in weighted_queue_order(weights):
        job_id = client.lmove(queue, processing, "RIGHT", "LEFT")
        if job_id is not None:
            return job_id
    return None


def ack(client: redis.Redis, worker_id: str, job_id: str) -> None:
    client.lrem(PROCESSING_KEY.format(worker_id=worker_id), 1, job_id)


def queue_depths(client: redis.Redis) -> dict[str, int]:
    return {queue.rsplit(":", 1)[-1]: client.llen(queue) for queue in QUEUES}
