"""backend/app/api/routers/dataset.py

Dataset save HTTP routes.

v0.3 endpoints
-------------
POST /dataset/save       (multipart: image/mask/scene/label)
POST /dataset/save_json  (JSON: base64 image/mask + objects)

Responsibilities:
- Persist a sample under DATASET_ROOT/<sample_id>/
- Update manifest.jsonl

Implementation calls services/storage + services/exporter + services/manifest
if available. Those modules can be implemented later; this router assumes their
interfaces match the v0.3 design.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..deps import get_settings


router = APIRouter(tags=["dataset"])


# -------------------------
# Fallback request models
# -------------------------

try:
    from ..schemas.requests import DatasetSaveJsonRequest  # type: ignore
except Exception:  # pragma: no cover
    from pydantic import BaseModel

    class DatasetSaveJsonRequest(BaseModel):
        image_png_base64: str
        mask_png_base64: str
        scene: Dict[str, Any]
        label: Dict[str, Any]
        sample_id: Optional[str] = None


# -------------------------
# Helpers
# -------------------------

def _error(code: str, message: str, details: Optional[Dict[str, Any]] = None, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _strip_b64_prefix(s: str) -> str:
    s = (s or "").strip()
    if "," in s and s.lower().startswith("data:"):
        return s.split(",", 1)[1]
    return s


def _b64_to_bytes(s: str) -> bytes:
    try:
        return base64.b64decode(_strip_b64_prefix(s), validate=False)
    except Exception as e:
        raise _error("MASK_DECODE_ERROR", "base64 decode failed", {"error": str(e)})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_manifest_record(*, sample_id: str, saved_paths: Dict[str, Any], scene_obj: Dict[str, Any], label_obj: Dict[str, Any], settings) -> Dict[str, Any]:
    meta = (scene_obj or {}).get("meta", {}) if isinstance(scene_obj, dict) else {}
    return {
        "sample_id": sample_id,
        "paths": saved_paths,
        "function": (label_obj or {}).get("function"),
        "counts_visible": (label_obj or {}).get("counts_visible"),
        "seed": meta.get("seed"),
        "tool_version": getattr(settings, "TOOL_VERSION", "0.3") if settings is not None else "0.3",
        "vocab_version": meta.get("vocab_version"),
        "timestamp": _now_iso(),
    }


def _save_via_services(
    *,
    request: Request,
    image_bytes: bytes,
    mask_bytes: bytes,
    scene_obj: Dict[str, Any],
    label_obj: Dict[str, Any],
    sample_id: Optional[str],
) -> Dict[str, Any]:
    """Save a sample using the v0.3 service layer (preferred)."""

    try:
        settings = get_settings(request)
    except HTTPException as exc:
        raise _error("SAVE_FAILED", "Settings not available", {"dependency_error": exc.detail}, status_code=500)

    try:
        from ...services.storage import LocalStorage  # type: ignore
        from ...services.exporter import save_sample  # type: ignore
        from ...services.manifest import append_record  # type: ignore
    except Exception as e:
        # Allow early development without services implemented yet.
        raise _error(
            "SAVE_FAILED",
            "Storage services not available (implement backend/app/services/*)",
            {"error": str(e)},
            status_code=501,
        )

    storage = LocalStorage(settings.DATASET_ROOT)

    try:
        save_out = save_sample(
            storage,
            image_bytes=image_bytes,
            mask_bytes=mask_bytes,
            scene_obj=scene_obj,
            label_obj=label_obj,
            sample_id=sample_id,
        )
    except Exception as e:
        raise _error("SAVE_FAILED", "save_sample failed", {"error": str(e)}, status_code=500)

    # Expected: save_out contains at least sample_id + saved_paths.
    sid = save_out.get("sample_id") or sample_id
    if not sid:
        raise _error("SAVE_FAILED", "save_sample did not return sample_id", {"save_out": save_out}, status_code=500)

    saved_paths = save_out.get("paths") or save_out.get("saved_paths") or save_out.get("saved") or {}

    # Append to manifest.jsonl
    try:
        record = _build_manifest_record(
            sample_id=sid,
            saved_paths=saved_paths,
            scene_obj=scene_obj,
            label_obj=label_obj,
            settings=settings,
        )
        append_record(settings.MANIFEST_PATH, record)
    except Exception as e:
        # Saving files succeeded but manifest append failed.
        raise _error("SAVE_FAILED", "manifest append failed", {"error": str(e), "sample_id": sid}, status_code=500)

    return {"ok": True, "sample_id": sid, "saved_paths": saved_paths}


# -------------------------
# Routes
# -------------------------


@router.post("/dataset/save")
async def dataset_save_multipart(
    request: Request,
    image: UploadFile = File(...),
    mask: UploadFile = File(...),
    scene: UploadFile = File(...),
    label: UploadFile = File(...),
    sample_id: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """multipart: persist sample and update manifest."""

    try:
        image_bytes = await image.read()
        mask_bytes = await mask.read()
        scene_bytes = await scene.read()
        label_bytes = await label.read()

        scene_obj = json.loads(scene_bytes.decode("utf-8"))
        label_obj = json.loads(label_bytes.decode("utf-8"))

        if not image_bytes:
            raise _error("SAVE_FAILED", "image is empty")
        if not mask_bytes:
            raise _error("SAVE_FAILED", "mask is empty")

        return _save_via_services(
            request=request,
            image_bytes=image_bytes,
            mask_bytes=mask_bytes,
            scene_obj=scene_obj,
            label_obj=label_obj,
            sample_id=sample_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _error("SAVE_FAILED", "dataset save failed", {"error": str(e)}, status_code=500)


@router.post("/dataset/save_json")
def dataset_save_json(req: DatasetSaveJsonRequest, request: Request) -> Dict[str, Any]:
    """JSON(base64): persist sample and update manifest."""

    try:
        image_bytes = _b64_to_bytes(getattr(req, "image_png_base64"))
        mask_bytes = _b64_to_bytes(getattr(req, "mask_png_base64"))
        scene_obj = getattr(req, "scene")
        label_obj = getattr(req, "label")
        sample_id = getattr(req, "sample_id", None)

        return _save_via_services(
            request=request,
            image_bytes=image_bytes,
            mask_bytes=mask_bytes,
            scene_obj=scene_obj,
            label_obj=label_obj,
            sample_id=sample_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _error("SAVE_FAILED", "dataset save_json failed", {"error": str(e)}, status_code=500)
