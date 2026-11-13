"""Allowlisted job types. Clients choose a registered handler by name; they can
never submit code. The API uses the same registry to validate payloads."""
import importlib
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from workers.context import JobContext
from workers.errors import UnknownJobTypeError

HandlerFunc = Callable[[BaseModel, JobContext], dict]


@dataclass(frozen=True)
class HandlerSpec:
    name: str
    func: HandlerFunc
    payload_model: type[BaseModel]
    timeout_seconds: float


_REGISTRY: dict[str, HandlerSpec] = {}
_loaded = False


def job_handler(name: str, payload_model: type[BaseModel], timeout_seconds: float = 60.0):
    def decorator(func: HandlerFunc) -> HandlerFunc:
        if name in _REGISTRY:
            raise ValueError(f"Duplicate job handler: {name}")
        _REGISTRY[name] = HandlerSpec(name, func, payload_model, timeout_seconds)
        return func
    return decorator


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        importlib.import_module("workers.handlers")
        _loaded = True


def get_handler(job_type: str) -> HandlerSpec:
    _ensure_loaded()
    spec = _REGISTRY.get(job_type)
    if spec is None:
        raise UnknownJobTypeError(f"Unsupported job type: {job_type}")
    return spec


def registered_job_types() -> list[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def job_type_schemas() -> list[dict]:
    """Payload JSON schemas, so clients can build input forms per job type."""
    _ensure_loaded()
    return [
        {
            "job_type": spec.name,
            "timeout_seconds": spec.timeout_seconds,
            "description": (spec.func.__doc__ or "").strip().split("\n\n")[0] or None,
            "payload_schema": spec.payload_model.model_json_schema(),
        }
        for spec in sorted(_REGISTRY.values(), key=lambda s: s.name)
    ]


def validate_payload(job_type: str, payload: dict) -> dict:
    """Validate and normalise a payload. Raises UnknownJobTypeError or pydantic.ValidationError."""
    spec = get_handler(job_type)
    return spec.payload_model.model_validate(payload).model_dump(mode="json")
