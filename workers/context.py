import threading
import uuid
from dataclasses import dataclass, field

from workers.errors import JobCancelled, JobTimeout, WorkerShutdown

STOP_CANCEL = "cancel"
STOP_TIMEOUT = "timeout"
STOP_SHUTDOWN = "shutdown"
STOP_LEASE_LOST = "lease_lost"


@dataclass
class JobContext:
    """Passed to handlers. Long-running handlers should call `check()` or use
    `sleep()` regularly so cancellation, timeouts and shutdown are honoured."""

    job_id: uuid.UUID
    tenant_id: int
    attempt_number: int
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _stop_reason: str | None = None

    def request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop_event.set()

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def check(self) -> None:
        if not self._stop_event.is_set():
            return
        if self._stop_reason == STOP_CANCEL:
            raise JobCancelled("Job was cancelled")
        if self._stop_reason == STOP_TIMEOUT:
            raise JobTimeout("Job exceeded its execution timeout")
        raise WorkerShutdown(f"Execution interrupted: {self._stop_reason}")

    def sleep(self, seconds: float) -> None:
        self._stop_event.wait(seconds)
        self.check()
