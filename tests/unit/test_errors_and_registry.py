import pydantic
import pytest

from workers.errors import (
    JobTimeout,
    PermanentError,
    RetryableError,
    UnknownJobTypeError,
    WorkerShutdown,
    classify,
)
from workers.registry import get_handler, registered_job_types, validate_payload


@pytest.mark.parametrize("exc,expected", [
    (RetryableError("x"), ("RetryableError", True)),
    (PermanentError("x"), ("PermanentError", False)),
    (JobTimeout("x"), ("Timeout", True)),
    (WorkerShutdown("x"), ("WorkerShutdown", True)),
    (UnknownJobTypeError("x"), ("UnknownJobType", False)),
    (ConnectionError("x"), ("ConnectionError", True)),
    (RuntimeError("x"), ("RuntimeError", True)),
])
def test_classify(exc, expected):
    assert classify(exc) == expected


def test_validation_errors_are_permanent():
    with pytest.raises(pydantic.ValidationError) as info:
        validate_payload("generate_report", {"format": "pdf"})
    assert classify(info.value) == ("InvalidPayload", False)


def test_builtin_job_types_registered():
    assert {"generate_report", "process_file", "send_notification", "simulated_work"} <= set(registered_job_types())


def test_unknown_job_type_rejected():
    with pytest.raises(UnknownJobTypeError):
        get_handler("os.system")


def test_payload_normalised_with_defaults():
    assert validate_payload("generate_report", {"report_id": "r1"}) == {
        "report_id": "r1", "format": "csv", "rows": 1500,
    }


def test_unexpected_payload_fields_rejected():
    with pytest.raises(pydantic.ValidationError):
        validate_payload("generate_report", {"report_id": "r1", "code": "import os"})
