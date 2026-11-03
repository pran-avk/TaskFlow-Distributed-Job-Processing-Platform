import json
import logging
import re
import sys

from core.config import get_settings

_SECRET_PATTERNS = [
    (re.compile(r"(token=)[^&\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.=]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(password[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(://[^:/\s]+:)[^@\s]+(@)"), r"\1***\2"),
]


def sanitize(text: str, max_length: int = 1000) -> str:
    """Redact credentials and neutralise newlines to prevent log injection."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > max_length:
        text = text[:max_length] + "...[truncated]"
    return text


class HideTokenFilter(logging.Filter):
    def filter(self, record):
        if record.args and len(record.args) >= 3:
            args = list(record.args)
            args[2] = re.sub(r"(token=)[^&\s]+", r"\1***", str(args[2]))
            record.args = tuple(args)
        return True


class JsonFormatter(logging.Formatter):
    _EXTRA_FIELDS = ("job_id", "job_type", "worker_id", "attempt", "status", "event_id")

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize(record.getMessage()),
        }
        for field in self._EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = str(value)
        if record.exc_info:
            entry["exception"] = sanitize(self.formatException(record.exc_info), 4000)
        return json.dumps(entry)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize(super().format(record), 4000)


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").addFilter(HideTokenFilter())
