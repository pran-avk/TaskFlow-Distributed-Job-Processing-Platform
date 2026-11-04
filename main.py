import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import REGISTRY

from core.config import get_settings
from core.logging import configure_logging
from database import SessionLocal
from jobs.api.jobs import router as jobs_router
from ops.api import router as ops_router
from ops.metrics import QueueStateCollector
from taskqueue.redis_client import get_redis
from tests import testuser
from users.signup.api.auth import Router as signup_router
from users.login.api.auth import router as login_router

# Tables are created by Alembic migrations: `alembic upgrade head`.

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="TaskFlow",
    description="Distributed background job processing platform",
    version="0.1.0",
)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.max_request_bytes:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    # Log the details server-side; never return stack traces or internals to clients.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(signup_router)
app.include_router(login_router)
app.include_router(testuser.router)
app.include_router(jobs_router)
app.include_router(ops_router)

_collector = QueueStateCollector(SessionLocal, get_redis, settings.worker_heartbeat_interval_seconds * 3)
try:
    REGISTRY.register(_collector)
except ValueError:
    pass  # already registered (module re-imported, e.g. by --reload)
