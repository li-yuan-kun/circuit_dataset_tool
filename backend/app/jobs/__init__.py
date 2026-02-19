"""backend/app/jobs/__init__.py

Jobs package (optional).

This package is imported by the optional router:
`backend/app/api/routers/jobs.py`.

Exposes a small stable surface:
- submit_job(job_type, payload) -> job_id
- get_job_status(job_id) -> status dict

Other helpers are exported for convenience in development.
"""

from __future__ import annotations

from .tasks import get_job_status, run_batch_mask, run_batch_shuffle, submit_job
from .worker import run_worker
from .scheduler import start_scheduler

__all__ = [
    "submit_job",
    "get_job_status",
    "run_batch_shuffle",
    "run_batch_mask",
    "run_worker",
    "start_scheduler",
]