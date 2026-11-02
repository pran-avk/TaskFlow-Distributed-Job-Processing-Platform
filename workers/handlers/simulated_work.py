from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from workers.context import JobContext
from workers.errors import PermanentError, RetryableError
from workers.registry import job_handler


class SimulatedWorkPayload(BaseModel):
    """Configurable workload for demos, benchmarks and failure testing."""

    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(default=100, ge=0, le=120_000)
    # Fail every attempt up to and including this attempt number.
    fail_until_attempt: int = Field(default=0, ge=0, le=20)
    failure: Literal["transient", "permanent"] = "transient"


@job_handler("simulated_work", SimulatedWorkPayload, timeout_seconds=60)
def simulated_work(payload: SimulatedWorkPayload, ctx: JobContext) -> dict:
    ctx.sleep(payload.duration_ms / 1000)
    if ctx.attempt_number <= payload.fail_until_attempt:
        if payload.failure == "permanent":
            raise PermanentError("Simulated permanent failure")
        raise RetryableError(f"Simulated transient failure on attempt {ctx.attempt_number}")
    return {"slept_ms": payload.duration_ms, "attempt": ctx.attempt_number}
