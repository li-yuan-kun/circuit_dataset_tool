"""backend/app/api/routers/jobs.py (optional)

Job-related endpoints.

v0.3 (optional) endpoints
------------------------
POST /jobs
GET  /jobs/{job_id}
GET  /jobs/{job_id}/download

This router is only mounted when Settings.ENABLE_JOBS is true.
The actual job queue/worker implementation lives in backend/app/jobs/.

During early development, if the jobs module is not implemented yet, these
endpoints return 501.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["jobs"])


def _raise(code: str, message: str, details: Dict[str, Any] | None = None, status_code: int = 400) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _require_jobs_impl():
    try:
        from ...jobs import submit_job as _submit_job  # type: ignore
        from ...jobs import get_job_status as _get_job_status  # type: ignore

        return _submit_job, _get_job_status
    except Exception:
        return None


@router.post("/jobs")
def submit_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    impl = _require_jobs_impl()
    if impl is None:
        _raise("JOBS_NOT_IMPLEMENTED", "Jobs feature is enabled but backend/app/jobs is not implemented yet", status_code=501)
    _submit_job, _ = impl

    job_type = str(payload.get("job_type") or payload.get("type") or "generic")
    job_id = _submit_job(job_type=job_type, payload=payload)  # type: ignore
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_status(job_id: str) -> Dict[str, Any]:
    impl = _require_jobs_impl()
    if impl is None:
        _raise("JOBS_NOT_IMPLEMENTED", "Jobs feature is enabled but backend/app/jobs is not implemented yet", status_code=501)
    _, _get_job_status = impl

    st = _get_job_status(job_id)  # type: ignore
    if not st:
        _raise("JOB_NOT_FOUND", f"Job not found: {job_id}", status_code=404)
    return st


@router.get("/jobs/{job_id}/download")
def download(job_id: str) -> Dict[str, Any]:
    """Return download info for a finished job.

    The exact contract may evolve. For now, we forward whatever the jobs backend
    exposes in the status `result` field.
    """

    impl = _require_jobs_impl()
    if impl is None:
        _raise("JOBS_NOT_IMPLEMENTED", "Jobs feature is enabled but backend/app/jobs is not implemented yet", status_code=501)
    _, _get_job_status = impl

    st = _get_job_status(job_id)  # type: ignore
    if not st:
        _raise("JOB_NOT_FOUND", f"Job not found: {job_id}", status_code=404)

    result = st.get("result") if isinstance(st, dict) else None
    if not result:
        _raise("JOB_NOT_READY", "Job result is not available", details={"job_id": job_id}, status_code=409)

    # Expected shape per design: {download_url|paths}
    if isinstance(result, dict):
        if "download_url" in result or "paths" in result:
            return result

    return {"result": result}
