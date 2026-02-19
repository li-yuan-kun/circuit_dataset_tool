"""backend/app/api/routers/topology.py

Topology (layout shuffle) HTTP routes.

v0.3 endpoint
-------------
POST /topology/shuffle

This endpoint randomizes node placement while keeping netlist connectivity
unchanged. It calls core_logic.topology.shuffle_scene() and then verifies
topology invariance.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["topology"])


# ---------------------------------------------------------------------------
# Fallback request model
# ---------------------------------------------------------------------------

try:
    from ..schemas.requests import ShuffleSceneRequest  # type: ignore
except Exception:

    class ShuffleSceneRequest(BaseModel):
        scene: Dict[str, Any]
        params: Dict[str, Any] = Field(default_factory=dict)
        return_paths: bool = True


def _error(code: str, message: str, details: Optional[Dict[str, Any]] = None, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _get_settings(request: Request):
    s = getattr(request.app.state, "settings", None)
    if s is None:
        try:
            from ...config import get_settings  # type: ignore

            s = get_settings()
        except Exception:
            s = None
    return s


def _get_vocab(request: Request):
    vocab = getattr(request.app.state, "vocab", None)
    if vocab is not None:
        return vocab

    settings = _get_settings(request)
    if settings is None:
        return None

    try:
        from ...core_logic.rasterize import load_vocab  # type: ignore

        vocab = load_vocab(settings.VOCAB_PATH)
        request.app.state.vocab = vocab
        return vocab
    except Exception:
        return None


@router.post("/topology/shuffle")
def topology_shuffle(req: ShuffleSceneRequest, request: Request) -> Dict[str, Any]:
    scene: Dict[str, Any] = req.scene
    params: Dict[str, Any] = getattr(req, "params", {}) or {}
    return_paths: bool = bool(getattr(req, "return_paths", True))

    vocab = _get_vocab(request)
    if vocab is None:
        raise _error("VOCAB_MISMATCH", "Vocab is not loaded; cannot shuffle scene", status_code=500)

    # Seed: prefer scene.meta.seed, fallback to 0.
    seed = 0
    try:
        seed = int((scene.get("meta") or {}).get("seed") or 0)
    except Exception:
        seed = 0

    try:
        from ...core_logic.topology import shuffle_scene, verify_topology_invariant  # type: ignore

        scene_shuffled, meta = shuffle_scene(scene=scene, vocab=vocab, params=params, seed=seed, return_paths=return_paths)
        verify_topology_invariant(scene, scene_shuffled)

    except HTTPException:
        raise
    except Exception as e:
        # Try to map common invariant failures.
        msg = str(e)
        if "invariant" in msg.lower() or "topology" in msg.lower():
            raise _error(
                "TOPOLOGY_INVARIANT_BROKEN",
                "Topology invariant broken after shuffle",
                details={"error": msg},
                status_code=400,
            )
        raise _error("TOPOLOGY_SHUFFLE_FAILED", "Shuffle failed", details={"error": msg}, status_code=500)

    # Ensure meta is JSON-serializable.
    try:
        json.dumps(meta)
    except Exception:
        meta = {"note": "meta is not JSON-serializable"}

    return {"scene_shuffled": scene_shuffled, "meta": meta}
