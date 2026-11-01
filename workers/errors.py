import pydantic


class RetryableError(Exception):
    """Transient failure: network blips, rate limits, temporary unavailability."""


class PermanentError(Exception):
    """Failure that retrying cannot fix: invalid input, unsupported operation."""


class JobTimeout(RetryableError):
    pass


class WorkerShutdown(RetryableError):
    pass


class JobCancelled(Exception):
    pass


class UnknownJobTypeError(PermanentError):
    pass


def classify(exc: BaseException) -> tuple[str, bool]:
    """Return (error_type, retryable) for an exception raised by a handler."""
    if isinstance(exc, JobTimeout):
        return "Timeout", True
    if isinstance(exc, WorkerShutdown):
        return "WorkerShutdown", True
    if isinstance(exc, UnknownJobTypeError):
        return "UnknownJobType", False
    if isinstance(exc, pydantic.ValidationError):
        return "InvalidPayload", False
    if isinstance(exc, PermanentError):
        return type(exc).__name__, False
    if isinstance(exc, RetryableError):
        return type(exc).__name__, True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return type(exc).__name__, True
    # Unknown failures default to retryable; max_attempts bounds the cost of being wrong.
    return type(exc).__name__, True
