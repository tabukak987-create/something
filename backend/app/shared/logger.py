"""
Shared Structlog Configuration
Provides a structured, JSON-capable logger used by every service in the gateway.
"""

import logging
import os
import sys
import structlog
from structlog.types import Processor


def _configure_stdlib_logging(log_level: str = "INFO") -> None:
    """Wire stdlib logging so legacy code (e.g. uvicorn) emits via structlog."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )
    # Forward all stdlib loggers through structlog's renderer
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("uvicorn.error").propagate = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def configure_logging(
    service_name: str = "unified_gateway",
    log_level: str | None = None,
    json: bool | None = None,
) -> structlog.BoundLogger:
    """
    Bootstrap structlog once for the whole process.

    Parameters
    ----------
    service_name:  Short label injected into every log record.
    log_level:     Override; defaults to LOG_LEVEL env-var or "INFO".
    json:          Force JSON output; auto-detected from LOG_JSON env-var.
    """
    level = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
    use_json = json if json is not None else os.getenv("LOG_JSON", "0") == "1"

    _configure_stdlib_logging(level)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if use_json:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty(),
            exception_formatter=structlog.dev.plain_traceback,
        )

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return get_logger(service_name)


def get_logger(name: str = "app") -> structlog.BoundLogger:
    """Return a structlog bound-logger pre-tagged with *name*."""
    return structlog.get_logger(name)
