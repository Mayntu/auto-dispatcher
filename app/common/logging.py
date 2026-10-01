"""JSON logging via structlog (CLAUDE.md §20).

Existing modules use the stdlib `logging` module; `setup_logging` routes both stdlib and
structlog records through one JSON formatter and binds the `service` field to every line.
If structlog is not installed (minimal dev env) it falls back to a plain text format.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(service: str, level: str = "INFO") -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    try:
        import structlog
        from structlog.contextvars import bind_contextvars
    except Exception:  # structlog absent -> plain logs, never crash the service
        logging.basicConfig(level=lvl, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    bind_contextvars(service=service)
