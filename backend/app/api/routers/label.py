"""backend/app/api/routers/label.py

Label-related HTTP routes.

v0.3 endpoint
-------------
POST /label/compute

Input: scene + mask (png base64) + occ_threshold + function
Output: {label}

This router handles decoding the PNG mask, basic resolution checks, and calls
core_logic.occlusion.compute_label().
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any, Dict, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Request

from ..deps import get_footprint_db, get_settings

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover
    BaseModel = object  # type: ignore
    Field = lambda default=None, **kwargs: default  # type: ignore


try:
    from ..schemas.requests import ComputeLabelRequest
except Exception:  # Fallback for early development

    class ComputeLabelRequest(BaseModel):
        scene: Dict[str, Any]
        mask_png_base64: str
        occ_threshold: Optional[float] = None
        function: str = ""


router = APIRouter(tags=["label"])


def _raise(code: str, message: str, details: Optional[Dict[str, Any]] = None, status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _decode_png_base64_to_mask(mask_png_base64: str) -> np.ndarray:
    s = (mask_png_base64 or "").strip()
    if not s:
        _raise("MASK_DECODE_ERROR", "mask_png_base64 is empty", status_code=400)

    if "," in s and s.lower().startswith("data:image"):
        s = s.split(",", 1)[1]

    try:
        raw = base64.b64decode(s, validate=False)
    except Exception as e:
        _raise("MASK_DECODE_ERROR", "failed to base64-decode mask", details={"error": str(e)}, status_code=400)

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("L")
        arr = np.array(img, dtype=np.uint8)
        # binarize to 0/255
        arr = np.where(arr > 0, 255, 0).astype(np.uint8)
        return arr
    except Exception as e:
        _raise("MASK_DECODE_ERROR", "failed to decode PNG mask", details={"error": str(e)}, status_code=400)


@router.post("/label/compute")
def label_compute(req: ComputeLabelRequest, request: Request) -> Dict[str, Any]:
    scene = req.scene
    mask = _decode_png_base64_to_mask(req.mask_png_base64)

    # Occ threshold
    try:
        settings = get_settings(request)
    except HTTPException:
        settings = None
    occ_threshold = req.occ_threshold
    if occ_threshold is None:
        occ_threshold = getattr(settings, "DEFAULT_OCC_THRESHOLD", 0.9)

    try:
        occ_threshold = float(occ_threshold)
    except Exception:
        _raise("REQUEST_VALIDATION_ERROR", "occ_threshold must be a number", status_code=422)

    if not (0.0 <= occ_threshold <= 1.0):
        _raise("REQUEST_VALIDATION_ERROR", "occ_threshold must be within [0, 1]", status_code=422)

    func = (getattr(req, "function", None) or "").strip()
    if not func:
        _raise("REQUEST_VALIDATION_ERROR", "function must be a non-empty string", status_code=422)

    # Resolution check (if present)
    try:
        res = (scene.get("meta") or {}).get("resolution") or {}
        w = int(res.get("w"))
        h = int(res.get("h"))
        if mask.shape[1] != w or mask.shape[0] != h:
            _raise(
                "MASK_DECODE_ERROR",
                "mask resolution does not match scene.meta.resolution",
                details={"scene_w": w, "scene_h": h, "mask_w": int(mask.shape[1]), "mask_h": int(mask.shape[0])},
                status_code=400,
            )
    except Exception:
        # If scene doesn't provide resolution, skip strict check.
        pass

    try:
        footprint_db = get_footprint_db(request)
    except HTTPException as exc:
        _raise(
            "OCCLUSION_FAILED",
            "footprint_db is not available (resources not loaded)",
            details={"dependency_error": exc.detail},
            status_code=500,
        )

    try:
        from ...core_logic.occlusion import compute_label  # type: ignore

        label_obj = compute_label(scene=scene, mask=mask, footprint_db=footprint_db, function=func, occ_threshold=occ_threshold)
    except HTTPException:
        raise
    except Exception as e:
        _raise("OCCLUSION_FAILED", "failed to compute label", details={"error": str(e)}, status_code=500)

    # Optional: ensure label_version
    if isinstance(label_obj, dict):
        label_obj.setdefault("label_version", "0.3")
        label_obj.setdefault("occ_threshold", occ_threshold)
        label_obj.setdefault("function", func)

    return {"label": label_obj}
