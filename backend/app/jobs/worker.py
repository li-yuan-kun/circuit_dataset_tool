"""backend/app/jobs/worker.py  (optional)

Single-process background worker.

The Jobs API router (`backend/app/api/routers/jobs.py`) calls the functions
exported by this package (`submit_job`, `get_job_status`). `submit_job` enqueues
work here; the worker consumes the queue and executes jobs via
`backend/app/jobs/tasks.execute_job()`.

This implementation is intentionally simple (v0.3/v0.4):
- In-memory queue
- A single daemon thread started on-demand

If you later move to an external queue system (Redis/Celery/RQ), you can keep
`tasks.execute_job()` and swap out this worker implementation.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, Optional, Tuple

try:
    from ..logging import get_logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore
        return logging.getLogger(name)


logger = get_logger(__name__)

# (job_id, job_type, payload)
JobItem = Tuple[str, str, Dict[str, Any]]

_QUEUE: "queue.Queue[JobItem]" = queue.Queue()
_STOP_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_LOCK = threading.Lock()


def enqueue_job(job_id: str, job_type: str, payload: Dict[str, Any]) -> None:
    """Enqueue a job for the background worker."""
    _QUEUE.put((str(job_id), str(job_type), dict(payload or {})))


def ensure_worker_started() -> None:
    """Start the background worker thread if not already running."""
    global _WORKER_THREAD

    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return

        _STOP_EVENT.clear()
        t = threading.Thread(target=_worker_loop, name="cdt-jobs-worker", daemon=True)
        t.start()
        _WORKER_THREAD = t
        logger.info("jobs_worker_started")


def stop_worker() -> None:
    """Signal the daemon worker thread to stop (best-effort)."""
    _STOP_EVENT.set()


def run_worker() -> None:
    """Run the worker loop in the current thread (blocking).

    This is mainly useful for development if you want to run a dedicated worker
    process.
    """
    _STOP_EVENT.clear()
    _worker_loop()


def _worker_loop() -> None:
    """Internal worker loop."""
    from .tasks import execute_job  # local import to avoid circular imports

    while not _STOP_EVENT.is_set():
        try:
            job_id, job_type, payload = _QUEUE.get(timeout=0.25)
        except queue.Empty:
            continue

        try:
            execute_job(job_id=job_id, job_type=job_type, payload=payload)
        except Exception as e:  # pragma: no cover
            # execute_job already records a failed status; we just avoid crashing.
            logger.exception(
                "jobs_worker_unhandled",
                extra={"job_id": job_id, "job_type": job_type, "error": str(e)},
            )
        finally:
            try:
                _QUEUE.task_done()
            except Exception:
                pass