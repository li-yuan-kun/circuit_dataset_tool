"""backend/app/api/routers/scene.py

Scene-related HTTP routes.

v0.3 endpoint
-------------
POST /scene/validate

This router keeps validation lightweight:
- Basic shape checks for meta/nodes/nets
- Vocab consistency checks (type exists, pins exist)
- Optional JSON Schema validation when `jsonschema` and `shared/scene.schema.json`
  are available.

It also normalizes the scene by filling common defaults (rot/scale/path).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, Request

try:
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    BaseModel = object  # type: ignore


# ---------------------------------------------------------------------------
# Fallback request schema (if api/schemas are not ready yet)
# ---------------------------------------------------------------------------

try:
    from ..schemas.requests import ValidateSceneRequest  # type: ignore
except Exception:  # pragma: no cover

    class ValidateSceneRequest(BaseModel):  # type: ignore
        scene: Dict[str, Any]
        strict: bool = False


router = APIRouter(tags=["scene"])


def _http_error(code: str, message: str, *, details: Optional[Dict[str, Any]] = None, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def _get_settings(request: Request):
    s = getattr(request.app.state, "settings", None)
    if s is not None:
        return s
    try:
        from ...config import get_settings  # type: ignore

        return get_settings()
    except Exception:
        return None


def _get_vocab(request: Request) -> Optional[Dict[str, Any]]:
    v = getattr(request.app.state, "vocab", None)
    if isinstance(v, dict):
        return v

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


def _try_schema_validate(scene: Dict[str, Any], request: Request, warnings: List[str]) -> None:
    """Try validating scene.json with JSON Schema if possible."""

    settings = _get_settings(request)
    vocab = _get_vocab(request)
    schema_path: Optional[Path] = None

    # Derive schema path from vocab location if possible.
    try:
        if settings is not None and hasattr(settings, "VOCAB_PATH"):
            schema_path = Path(settings.VOCAB_PATH).parent / "scene.schema.json"
        elif isinstance(vocab, dict) and "vocab_path" in vocab:
            schema_path = Path(str(vocab["vocab_path"])).parent / "scene.schema.json"
    except Exception:
        schema_path = None

    if not schema_path or not schema_path.exists():
        return

    try:
        import jsonschema  # type: ignore

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=scene, schema=schema)
    except ImportError:
        warnings.append("jsonschema not installed; skipped JSON Schema validation")
    except Exception as e:
        raise _http_error(
            "SCENE_SCHEMA_ERROR",
            "scene does not satisfy JSON Schema",
            details={"error": str(e), "schema_path": str(schema_path)},
            status_code=400,
        )


def _normalize_scene(scene: Dict[str, Any], settings) -> Dict[str, Any]:
    s = copy.deepcopy(scene)

    meta = s.setdefault("meta", {})
    meta.setdefault("scene_version", "0.3")
    meta.setdefault("tool_version", getattr(settings, "TOOL_VERSION", "0.3") if settings is not None else "0.3")
    meta.setdefault("params", {})

    # Fill resolution defaults if missing.
    res = meta.setdefault("resolution", {})
    if "w" not in res and settings is not None:
        res["w"] = getattr(settings, "DEFAULT_RESOLUTION_W", 1024)
    if "h" not in res and settings is not None:
        res["h"] = getattr(settings, "DEFAULT_RESOLUTION_H", 1024)

    # Normalize nodes.
    nodes = s.setdefault("nodes", [])
    for n in nodes:
        if isinstance(n, dict):
            n.setdefault("rot", 0)
            n.setdefault("scale", 1.0)

    # Normalize nets.
    nets = s.setdefault("nets", [])
    for e in nets:
        if isinstance(e, dict):
            e.setdefault("path", [])

    return s


def _validate_scene_basic(scene: Dict[str, Any], vocab: Optional[Dict[str, Any]], strict: bool) -> Tuple[List[str], List[str]]:
    """Return (warnings, errors)."""

    warnings: List[str] = []
    errors: List[str] = []

    if not isinstance(scene, dict):
        errors.append("scene must be an object")
        return warnings, errors

    if "meta" not in scene or not isinstance(scene.get("meta"), dict):
        warnings.append("scene.meta missing or invalid")
    if "nodes" not in scene or not isinstance(scene.get("nodes"), list):
        errors.append("scene.nodes missing or invalid")
    if "nets" not in scene or not isinstance(scene.get("nets"), list):
        errors.append("scene.nets missing or invalid")
    if errors:
        return warnings, errors

    nodes: List[Dict[str, Any]] = [n for n in scene.get("nodes", []) if isinstance(n, dict)]
    node_ids: Set[str] = set()
    node_type_by_id: Dict[str, str] = {}

    for n in nodes:
        nid = n.get("id")
        ntype = n.get("type")
        pos = n.get("pos")
        if not isinstance(nid, str) or not nid:
            errors.append("node.id missing/invalid")
            continue
        if nid in node_ids:
            errors.append(f"duplicate node.id: {nid}")
            continue
        node_ids.add(nid)

        if not isinstance(ntype, str) or not ntype:
            errors.append(f"node.type missing/invalid for node {nid}")
        else:
            node_type_by_id[nid] = ntype

        if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
            errors.append(f"node.pos missing/invalid for node {nid}")

        # vocab type check
        if vocab is not None:
            types = (vocab.get("types") or {}) if isinstance(vocab, dict) else {}
            if isinstance(types, dict) and isinstance(ntype, str) and ntype and ntype not in types:
                errors.append(f"VOCAB_MISMATCH: node {nid} has unknown type '{ntype}'")

    nets: List[Dict[str, Any]] = [e for e in scene.get("nets", []) if isinstance(e, dict)]
    for e in nets:
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            errors.append("net.id missing/invalid")

        for side in ("from", "to"):
            ep = e.get(side)
            if not isinstance(ep, dict):
                errors.append(f"net.{side} missing/invalid (net {eid})")
                continue
            nref = ep.get("node")
            pref = ep.get("pin")
            if not isinstance(nref, str) or nref not in node_ids:
                errors.append(f"net endpoint references unknown node '{nref}' (net {eid})")
                continue
            if not isinstance(pref, str) or not pref:
                errors.append(f"net endpoint pin missing/invalid for node '{nref}' (net {eid})")
                continue

            # pin existence in vocab
            if vocab is not None:
                types = vocab.get("types") or {}
                ntype = node_type_by_id.get(nref)
                pins: List[str] = []
                try:
                    pins = list((types.get(ntype, {}) or {}).get("pins") or [])
                except Exception:
                    pins = []
                if pins and pref not in pins:
                    errors.append(f"PIN_NOT_FOUND: node '{nref}' type '{ntype}' has no pin '{pref}'")

    if strict:
        # Strict mode: meta fields should exist.
        meta = scene.get("meta") or {}
        for k in ("scene_version", "tool_version", "vocab_version", "seed", "resolution", "timestamp"):
            if k not in meta:
                errors.append(f"meta.{k} missing (strict mode)")

    return warnings, errors


@router.post("/scene/validate")
def scene_validate(req: ValidateSceneRequest, request: Request) -> Dict[str, Any]:
    """Validate scene payload (schema + vocab consistency) and return normalized scene."""

    scene = getattr(req, "scene", None)
    if scene is None:
        raise _http_error("SCENE_SCHEMA_ERROR", "missing scene")
    if not isinstance(scene, dict):
        raise _http_error("SCENE_SCHEMA_ERROR", "scene must be an object")

    strict = bool(getattr(req, "strict", False))

    warnings: List[str] = []

    # Optional JSON Schema validation
    _try_schema_validate(scene, request, warnings)

    vocab = _get_vocab(request)
    w2, errs = _validate_scene_basic(scene, vocab, strict)
    warnings.extend(w2)
    if errs:
        # Pick error code.
        code = "SCENE_INVALID"
        if any(s.startswith("VOCAB_MISMATCH") for s in errs):
            code = "VOCAB_MISMATCH"
        elif any(s.startswith("PIN_NOT_FOUND") for s in errs):
            code = "PIN_NOT_FOUND"
        raise _http_error(code, "scene validation failed", details={"errors": errs}, status_code=400)

    settings = _get_settings(request)
    scene_norm = _normalize_scene(scene, settings)

    return {"ok": True, "scene_norm": scene_norm, "warnings": warnings}
