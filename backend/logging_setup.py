"""Process-safe application logging for the Paper Information Extraction service.

The web process owns the rotating file handlers.  ``multiprocessing`` workers
send ``LogRecord`` objects to the web process through a queue so that Windows
spawned workers never rotate the same file concurrently.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterator


_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "paper_log_context", default={}
)
_LISTENER: QueueListener | None = None
_LOG_QUEUE = None
_OWNED_HANDLERS: list[logging.Handler] = []
_CONFIGURED = False

_REDACTIONS = (
    # Keep the key name while removing its value from messages and tracebacks.
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,})\b"), "[REDACTED]"),
)


def _redact(value: str) -> str:
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class _SafeFormatter(logging.Formatter):
    """Formatter that also redacts exception text after traceback rendering."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


class _ContextFilter(logging.Filter):
    """Add stable correlation fields to every record before it is queued."""

    _fields = ("request_id", "task_id", "document_id", "part", "provider")

    def filter(self, record: logging.LogRecord) -> bool:
        context = _CONTEXT.get()
        for field in self._fields:
            if not hasattr(record, field):
                setattr(record, field, context.get(field, "-"))
        return True


def bind_log_context(**values: Any):
    """Bind non-empty correlation values to the current async/thread context."""
    current = dict(_CONTEXT.get())
    for key, value in values.items():
        if value is not None and str(value) != "":
            current[key] = str(value)
    return _CONTEXT.set(current)


def reset_log_context(token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    token = bind_log_context(**values)
    try:
        yield
    finally:
        reset_log_context(token)


def _base_dir(config: Any | None) -> Path:
    path = getattr(config, "path", None)
    if path:
        # runtime.yaml lives in <repo>/backend/config/runtime.yaml.
        return Path(path).resolve().parent.parent
    return Path(__file__).resolve().parent


def _settings(config: Any | None) -> dict[str, Any]:
    value = getattr(config, "logging", {}) if config is not None else {}
    return value if isinstance(value, dict) else {}


def _level(value: Any) -> int:
    name = str(value or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _formatter() -> logging.Formatter:
    return _SafeFormatter(
        "%(asctime)s %(levelname)s [%(processName)s:%(process)d] "
        "[%(name)s] request_id=%(request_id)s task_id=%(task_id)s "
        "document_id=%(document_id)s part=%(part)s provider=%(provider)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _close_owned_handlers() -> None:
    for handler in list(_OWNED_HANDLERS):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
    _OWNED_HANDLERS.clear()


def configure_main_logging(config: Any | None = None):
    """Configure the main process and return the queue used by workers.

    The function is intentionally idempotent because development servers may
    import/reload the application more than once.
    """
    global _CONFIGURED, _LISTENER, _LOG_QUEUE
    settings = _settings(config)
    if settings.get("enabled", True) is False:
        _CONFIGURED = True
        return None
    if _CONFIGURED and _LISTENER is not None:
        return _LOG_QUEUE

    directory = Path(settings.get("directory", "data/logs"))
    if not directory.is_absolute():
        directory = _base_dir(config) / directory
    directory.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1024, int(settings.get("max_bytes", 30 * 1024 * 1024)))
    backup_count = max(1, int(settings.get("backup_count", 10)))
    level = _level(settings.get("level", "INFO"))
    # Spawned workers inherit this small non-secret setting through the
    # environment before they initialize their QueueHandler.
    os.environ["PAPER_LOG_LEVEL"] = logging.getLevelName(level)
    formatter = _formatter()
    context_filter = _ContextFilter()

    app_file = RotatingFileHandler(
        directory / "app.log", maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8", delay=True,
    )
    app_file.setLevel(level)
    app_file.setFormatter(formatter)
    app_file.addFilter(context_filter)
    access_file = RotatingFileHandler(
        directory / "access.log", maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8", delay=True,
    )
    access_file.setLevel(level)
    access_file.setFormatter(formatter)
    access_file.addFilter(context_filter)
    handlers: list[logging.Handler] = [app_file]
    if settings.get("console", True):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(formatter)
        console.addFilter(context_filter)
        handlers.append(console)
        _OWNED_HANDLERS.append(console)

    ctx = multiprocessing.get_context("spawn")
    _LOG_QUEUE = ctx.Queue(-1)
    listener = QueueListener(_LOG_QUEUE, *handlers, respect_handler_level=True)
    listener.start()
    _LISTENER = listener
    _OWNED_HANDLERS.extend([app_file, access_file])

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_paper_owned", False):
            root.removeHandler(handler)
    queue_handler = QueueHandler(_LOG_QUEUE)
    queue_handler._paper_owned = True
    queue_handler.addFilter(context_filter)
    root.addHandler(queue_handler)

    access_logger = logging.getLogger("paper.access")
    access_logger.setLevel(level)
    access_logger.propagate = False
    for handler in list(access_logger.handlers):
        if getattr(handler, "_paper_owned", False):
            access_logger.removeHandler(handler)
    access_file._paper_owned = True
    access_logger.addHandler(access_file)

    # The middleware below is the source of truth for access logs and adds the
    # request ID, so Uvicorn's duplicate access line is disabled.
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
    logging.getLogger("uvicorn.error").setLevel(level)
    for noisy_name in ("httpx", "httpcore", "sqlalchemy.engine", "multipart"):
        logging.getLogger(noisy_name).setLevel(max(level, logging.WARNING))
    logging.raiseExceptions = False
    _CONFIGURED = True
    return _LOG_QUEUE


def configure_worker_logging(log_queue=None):
    """Configure a spawned worker to forward records to the main process."""
    root = logging.getLogger()
    root.setLevel(_level(os.getenv("PAPER_LOG_LEVEL", "INFO")))
    for handler in list(root.handlers):
        if getattr(handler, "_paper_owned", False):
            root.removeHandler(handler)
    if log_queue is not None:
        handler: logging.Handler = QueueHandler(log_queue)
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_formatter())
    handler._paper_owned = True
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    logging.getLogger("uvicorn.access").disabled = True
    for noisy_name in ("httpx", "httpcore", "sqlalchemy.engine", "multipart"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)
    return handler


def get_log_queue():
    return _LOG_QUEUE


def shutdown_logging() -> None:
    """Flush and close the main-process listener and its rotating handlers."""
    global _CONFIGURED, _LISTENER, _LOG_QUEUE
    if _LISTENER is not None:
        try:
            _LISTENER.stop()
        except Exception:
            pass
        _LISTENER = None
    access_logger = logging.getLogger("paper.access")
    for handler in list(access_logger.handlers):
        if getattr(handler, "_paper_owned", False):
            access_logger.removeHandler(handler)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_paper_owned", False):
            root.removeHandler(handler)
    _close_owned_handlers()
    _LOG_QUEUE = None
    _CONFIGURED = False
