FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --system --uid 1000 taskflow && chown -R taskflow /app
USER taskflow

# Prometheus metrics for this worker process.
EXPOSE 9101
# SIGTERM triggers graceful shutdown; give it longer than WORKER_SHUTDOWN_GRACE_SECONDS.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "workers.runner"]
