"""backend/app/api/routers/mask.py

Mask-related HTTP routes.

v0.3 endpoint
-------------
POST /mask/generate

Returns either:
- JSON {mask_png_base64, meta}
- OR raw PNG bytes (image/png) when `return_bytes` is true.

If returned as bytes, a small meta payload is echoed in response headers:
  - x-mask-strategy
  - x-mask-seed-used
  - x-mask-meta (JSON, compact)
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..deps import get_settings

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover
    BaseModel = object  # type: ignore
    Field = lambda *a, **k: None  # type: ignore


# Optional shared request schema import.
try:
    from ..schemas.requests import GenerateMaskRequest  # type: ignore
except Exception:  # pragma: no cover

    class GenerateMaskRequest(BaseModel):
        scene: Dict[str, Any]
        strategy: str = Field(..., description="mask generation strategy")
        params: Dict[str, Any] = Field(default_factory=dict)
        # If true: return raw PNG bytes, otherwise return base64 in JSON.
        return_bytes: bool = False


router = APIRouter(tags=["mask"])


def _raise(code: str, message: str, details: Optional[Dict[str, Any]] = None, status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _scene_resolution(scene: Dict[str, Any], settings) -> Dict[str, int]:
    meta = scene.get("meta") or {}
    res = meta.get("resolution") or {}
    try:
        w = int(res.get("w") or getattr(settings, "DEFAULT_RESOLUTION_W", 1024) if settings else 1024)
        h = int(res.get("h") or getattr(settings, "DEFAULT_RESOLUTION_H", 1024) if settings else 1024)
        return {"w": w, "h": h}
    except Exception:
        return {"w": 1024, "h": 1024}


@router.post("/mask/generate")
def mask_generate(req: GenerateMaskRequest, request: Request):
    scene: Dict[str, Any] = req.scene  # type: ignore
    strategy = str(getattr(req, "strategy", "") or "").strip()
    params: Dict[str, Any] = dict(getattr(req, "params", None) or {})
    return_bytes = bool(getattr(req, "return_bytes", False))

    if not strategy:
        _raise("REQUEST_VALIDATION_ERROR", "Missing mask strategy", status_code=422)

    try:
        settings = get_settings(request)
    except HTTPException:
        settings = None
    resolution = _scene_resolution(scene, settings)
    seed = int((scene.get("meta") or {}).get("seed") or 0)

    try:
        from ...core_logic.mask_gen import (  # type: ignore
            decode_params_and_validate,
            encode_png,
            generate_mask,
        )
    except Exception as e:
        _raise("INTERNAL_ERROR", "mask_gen module is not available yet", details={"error": str(e)}, status_code=501)

    # Validate / default-fill params
    try:
        params_norm = decode_params_and_validate(strategy=strategy, params=params)
    except Exception as e:
        _raise("REQUEST_VALIDATION_ERROR", "Invalid mask params", details={"error": str(e)}, status_code=422)

    # Generate mask
    try:
        mask_np, meta = generate_mask(
            strategy=strategy,
            resolution=resolution,
            scene=scene,
            params=params_norm,
            seed=seed,
        )
        png_bytes: bytes = encode_png(mask_np)
    except Exception as e:
        _raise("MASK_GENERATE_FAILED", "Mask generation failed", details={"error": str(e)}, status_code=500)

    meta_out = dict(meta or {})
    meta_out.setdefault("strategy", strategy)
    meta_out.setdefault("params", params_norm)
    meta_out.setdefault("resolution", resolution)

    if return_bytes:
        # Keep meta compact to avoid oversized headers.
        meta_header = json.dumps(meta_out, ensure_ascii=True, separators=(",", ":"))
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "x-mask-strategy": strategy,
                "x-mask-seed-used": str(meta_out.get("seed_used", meta_out.get("seed", seed))),
                "x-mask-meta": meta_header[:8192],
            },
        )

    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {"mask_png_base64": b64, "meta": meta_out}
