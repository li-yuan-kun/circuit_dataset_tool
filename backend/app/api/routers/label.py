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
import time
from typing import Any, Dict, Optional

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

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


def _decode_png_bytes_to_mask(raw: bytes) -> np.ndarray:
    if not raw:
        _raise("MASK_DECODE_ERROR", "mask binary is empty", details={"stage": "decode"}, status_code=400)
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("L")
        arr = np.array(img, dtype=np.uint8)
        return np.where(arr > 0, 255, 0).astype(np.uint8)
    except Exception as e:
        _raise(
            "MASK_DECODE_ERROR",
            "failed to decode PNG mask",
            details={"error": str(e), "stage": "decode"},
            status_code=400,
        )


def _resolve_occ_threshold(req_occ_threshold: Optional[float], request: Request) -> float:
    try:
        settings = get_settings(request)
    except HTTPException:
        settings = None
    occ_threshold = req_occ_threshold
    if occ_threshold is None:
        occ_threshold = getattr(settings, "DEFAULT_OCC_THRESHOLD", 0.9)

    try:
        occ_threshold = float(occ_threshold)
    except Exception:
        _raise("REQUEST_VALIDATION_ERROR", "occ_threshold must be a number", details={"stage": "decode"}, status_code=422)

    if not (0.0 <= occ_threshold <= 1.0):
        _raise(
            "REQUEST_VALIDATION_ERROR",
            "occ_threshold must be within [0, 1]",
            details={"stage": "decode"},
            status_code=422,
        )
    return float(occ_threshold)


def _validate_scene_mask_resolution(scene: Dict[str, Any], mask: np.ndarray):
    try:
        res = (scene.get("meta") or {}).get("resolution") or {}
        w = int(res.get("w"))
        h = int(res.get("h"))
        if mask.shape[1] != w or mask.shape[0] != h:
            _raise(
                "MASK_DECODE_ERROR",
                "mask resolution does not match scene.meta.resolution",
                details={
                    "scene_w": w,
                    "scene_h": h,
                    "mask_w": int(mask.shape[1]),
                    "mask_h": int(mask.shape[0]),
                    "stage": "decode",
                },
                status_code=400,
            )
    except Exception:
        pass


def _compute_label_impl(scene: Dict[str, Any], mask: np.ndarray, func: str, occ_threshold: float, request: Request) -> Dict[str, Any]:
    try:
        footprint_db = get_footprint_db(request)
    except HTTPException as exc:
        _raise(
            "OCCLUSION_FAILED",
            "footprint_db is not available (resources not loaded)",
            details={"dependency_error": exc.detail, "stage": "render"},
            status_code=500,
        )

    try:
        from ...core_logic.occlusion import compute_label  # type: ignore

        return compute_label(scene=scene, mask=mask, footprint_db=footprint_db, function=func, occ_threshold=occ_threshold)
    except HTTPException:
        raise
    except Exception as e:
        _raise(
            "OCCLUSION_FAILED",
            "failed to compute label",
            details={"error": str(e), "stage": "intersect"},
            status_code=500,
        )


@router.post("/label/compute")
def label_compute(req: ComputeLabelRequest, request: Request) -> Dict[str, Any]:
    t0 = time.perf_counter()
    stage = "decode"
    scene = req.scene
    mask = _decode_png_base64_to_mask(req.mask_png_base64)

    occ_threshold = _resolve_occ_threshold(req.occ_threshold, request)

    func = (getattr(req, "function", None) or "").strip()
    if not func:
        _raise(
            "REQUEST_VALIDATION_ERROR",
            "function must be a non-empty string",
            details={"stage": stage},
            status_code=422,
        )

    _validate_scene_mask_resolution(scene, mask)

    t_decode = (time.perf_counter() - t0) * 1000.0
    stage = "render"
    t1 = time.perf_counter()
    label_obj = _compute_label_impl(scene=scene, mask=mask, func=func, occ_threshold=occ_threshold, request=request)
    t_compute = (time.perf_counter() - t1) * 1000.0
    stage = "assemble"
    t2 = time.perf_counter()

    if isinstance(label_obj, dict):
        label_obj.setdefault("label_version", "0.3")
        label_obj.setdefault("occ_threshold", occ_threshold)
        label_obj.setdefault("function", func)

    t_assemble = (time.perf_counter() - t2) * 1000.0
    logger = request.app.logger if hasattr(request.app, "logger") else None
    if logger:
        logger.info(
            "label_compute_timing",
            extra={
                "decode_ms": round(t_decode, 3),
                "render_ms": round(t_compute, 3),
                "intersect_ms": 0.0,
                "assemble_ms": round(t_assemble, 3),
                "mode": "json",
            },
        )

    return {"label": label_obj}


@router.post("/label/compute/binary")
async def label_compute_binary(
    request: Request,
    scene_json: str = Form(...),
    function: str = Form(...),
    occ_threshold: Optional[float] = Form(None),
    mask_png: UploadFile = File(...),
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        scene = json.loads(scene_json)
    except Exception as e:
        _raise(
            "REQUEST_VALIDATION_ERROR",
            "scene_json must be valid JSON",
            details={"error": str(e), "stage": "decode"},
            status_code=422,
        )

    raw = await mask_png.read()
    mask = _decode_png_bytes_to_mask(raw)
    occ_threshold_val = _resolve_occ_threshold(occ_threshold, request)
    func = (function or "").strip()
    if not func:
        _raise("REQUEST_VALIDATION_ERROR", "function must be a non-empty string", details={"stage": "decode"}, status_code=422)

    _validate_scene_mask_resolution(scene, mask)

    t_decode = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    label_obj = _compute_label_impl(scene=scene, mask=mask, func=func, occ_threshold=occ_threshold_val, request=request)
    t_compute = (time.perf_counter() - t1) * 1000.0

    if isinstance(label_obj, dict):
        label_obj.setdefault("label_version", "0.3")
        label_obj.setdefault("occ_threshold", occ_threshold_val)
        label_obj.setdefault("function", func)

    logger = request.app.logger if hasattr(request.app, "logger") else None
    if logger:
        logger.info(
            "label_compute_timing",
            extra={
                "decode_ms": round(t_decode, 3),
                "render_ms": round(t_compute, 3),
                "intersect_ms": 0.0,
                "assemble_ms": 0.0,
                "mode": "binary",
            },
        )

    return {"label": label_obj}
