"""backend/app/jobs/scheduler.py  (optional)

Optional scheduler for background maintenance.

For v0.3/v0.4 this module does two things:
- Ensure the background jobs worker is started.
- Optionally start a cleanup thread that evicts completed job records after a
  TTL, to avoid unbounded in-memory growth.

Env knobs (all optional)
------------------------
- CDT_JOBS_TTL_SECONDS:       default 86400 (24h)
- CDT_JOBS_CLEANUP_INTERVAL_SECONDS: default 600 (10min)
"""

from __future__ import annotations

import os
import threading
import time

try:
    from ..logging import get_logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore
        return logging.getLogger(name)


logger = get_logger(__name__)

_SCHED_LOCK = threading.Lock()
_SCHED_STARTED = False


def start_scheduler() -> None:
    """Start background maintenance threads (idempotent)."""
    global _SCHED_STARTED

    with _SCHED_LOCK:
        if _SCHED_STARTED:
            return
        _SCHED_STARTED = True

    # Always ensure the worker exists (submit_job also does this, but this makes
    # it available at startup if you choose to call start_scheduler() in lifespan).
    from .worker import ensure_worker_started

    ensure_worker_started()

    ttl = int(os.getenv("CDT_JOBS_TTL_SECONDS", "86400"))
    interval = int(os.getenv("CDT_JOBS_CLEANUP_INTERVAL_SECONDS", "600"))

    if ttl <= 0 or interval <= 0:
        logger.info("jobs_scheduler_disabled", extra={"ttl": ttl, "interval": interval})
        return

    t = threading.Thread(target=_cleanup_loop, name="cdt-jobs-cleanup", daemon=True, args=(ttl, interval))
    t.start()
    logger.info("jobs_scheduler_started", extra={"ttl": ttl, "interval": interval})


def _cleanup_loop(ttl_seconds: int, interval_seconds: int) -> None:
    from .tasks import cleanup_expired

    while True:
        try:
            removed = cleanup_expired(ttl_seconds)
            if removed:
                logger.info("jobs_cleanup", extra={"removed": removed})
        except Exception as e:  # pragma: no cover
            logger.warning("jobs_cleanup_failed", extra={"error": str(e)})
        time.sleep(float(interval_seconds))