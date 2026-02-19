"""backend/app/logging.py

Circuit Dataset Tool (v0.3) logging utilities.

This module provides:
  - setup_logging(): configure structured logging (level/format)
  - get_logger(name): get a module logger
  - register_request_id_middleware(app): inject request_id into a context var and
    write it to response headers.
  - request_timing_middleware(request, call_next): log latency and status_code.

Design notes
------------
The project wants "structured" logs. To keep dependencies minimal, this module
implements a small JSON formatter based on stdlib `logging`.

`request_id` propagation is implemented via `contextvars`, which works well with
async code and keeps request context out of global state.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Request, Response


# ---------------------------------------------------------------------------
# Context vars
# ---------------------------------------------------------------------------

_request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def _now_rfc3339() -> str:
    # RFC3339-ish timestamp with milliseconds, in UTC.
    dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coerce_jsonable(v: Any) -> Any:
    """Best-effort conversion of `v` into JSON-serializable values."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, bytes):
        # Avoid dumping raw bytes; keep it short.
        return f"<bytes len={len(v)}>"
    if isinstance(v, (list, tuple)):
        return [_coerce_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce_jsonable(val) for k, val in v.items()}
    # Fall back to string representation.
    try:
        return str(v)
    except Exception:
        return repr(v)


class _ContextFilter(logging.Filter):
    """Inject request-scoped fields into LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        rid = _request_id_var.get()
        if rid and not getattr(record, "request_id", None):
            setattr(record, "request_id", rid)
        return True


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter for structured logs."""

    # Fields that are part of base LogRecord (so we don't duplicate them in extras)
    _builtin_fields = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts": _now_rfc3339(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        # Add request_id if available.
        rid = getattr(record, "request_id", None) or _request_id_var.get()
        if rid:
            base["request_id"] = rid

        # Attach extras (anything passed via logger.*(…, extra={...}))
        extras: Dict[str, Any] = {}
        for k, v in record.__dict__.items():
            if k in self._builtin_fields:
                continue
            if k in {"message", "asctime"}:
                continue
            extras[k] = _coerce_jsonable(v)
        if extras:
            base.update(extras)

        # Exception information (if any)
        if record.exc_info:
            base["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            base["exc"] = self.formatException(record.exc_info)

        return json.dumps(base, ensure_ascii=False, separators=(",", ":"), default=_coerce_jsonable)


class _TextFormatter(logging.Formatter):
    """Human-readable text logs (useful for local dev)."""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", None) or _request_id_var.get()
        prefix = f"[{_now_rfc3339()}] {record.levelname:<7} {record.name}"
        if rid:
            prefix += f" rid={rid}"
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{prefix} - {msg}"


_LOGGING_CONFIGURED: bool = False


def setup_logging() -> None:
    """Configure structured logging.

    Environment variables (optional):
      - CDT_LOG_LEVEL: DEBUG/INFO/WARNING/ERROR (default: INFO)
      - CDT_LOG_FORMAT: json|text (default: json)

    This function is idempotent.
    """

    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    level_name = (os.getenv("CDT_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)

    fmt = (os.getenv("CDT_LOG_FORMAT") or "json").lower().strip()
    formatter: logging.Formatter = _JsonFormatter() if fmt != "text" else _TextFormatter()

    root = logging.getLogger()
    root.setLevel(level)

    # Reset handlers to avoid duplicate logs when running under reload.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)

    # Harmonize common server loggers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(level)

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module name."""

    if not _LOGGING_CONFIGURED:
        # Safe fallback if caller forgot to call setup_logging().
        setup_logging()
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------


def _pick_request_id(request: Request) -> str:
    """Pick request_id from inbound header/state/context or generate a new one."""

    rid = (
        request.headers.get("x-request-id")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "request_id", None)
        or _request_id_var.get()
    )
    return rid or str(uuid.uuid4())


def register_request_id_middleware(app: FastAPI) -> None:
    """Inject `request_id` into context + response header.

    - Accepts inbound request header `x-request-id` if provided.
    - Generates a UUID if missing.
    - Writes `x-request-id` to response headers.
    """

    logger = get_logger(__name__)

    @app.middleware("http")
    async def _request_id_mw(request: Request, call_next: Callable[[Request], Any]) -> Response:
        rid = _pick_request_id(request)
        token = _request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response: Response = await call_next(request)
        finally:
            # reset contextvar to avoid leaking across requests
            _request_id_var.reset(token)

        # Always echo request id.
        response.headers["x-request-id"] = rid
        # Also keep compatibility with some clients.
        response.headers.setdefault("X-Request-ID", rid)
        return response

    logger.debug("request_id_middleware_registered")


async def request_timing_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """Record request latency and status_code.

    Adds:
      - response header: x-latency-ms
      - log event: "request" with {request_id, method, path, status_code, latency_ms}
    """

    logger = get_logger(__name__)

    # Make sure there's a request_id in the context while we process this request.
    rid = _pick_request_id(request)
    prev = _request_id_var.get()
    token = _request_id_var.set(rid) if prev != rid else None
    request.state.request_id = rid
    t0 = time.perf_counter()
    response: Optional[Response] = None
    try:
        response = await call_next(request)
        return response
    finally:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        status_code = getattr(response, "status_code", 500)

        # Log after response is created (or in exception path).
        logger.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 3),
            },
        )

        if response is not None:
            response.headers["x-latency-ms"] = f"{latency_ms:.3f}"
            # Ensure request id exists even if request-id middleware isn't enabled.
            response.headers.setdefault("x-request-id", rid)

        if token is not None:
            _request_id_var.reset(token)
