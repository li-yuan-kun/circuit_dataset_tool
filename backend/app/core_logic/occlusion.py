"""backend/app/core_logic/occlusion.py

Occlusion (mask coverage) computation utilities.

This module implements the v0.3+ label generation logic described in the
design document:

  - For each node in a scene, estimate the occlusion ratio in [0,1] by
    intersecting the node's rasterized footprint with a binary mask.
  - Produce per-type counts for:
      * counts_all     : all nodes (ignoring mask)
      * counts_visible : nodes with occ_ratio < occ_threshold

The preferred method is pixel-based (footprint raster) because it is robust to
arbitrary irregular masks.

Notes
-----
* This module intentionally stays independent from FastAPI. Routers call
  :func:`compute_label`.
* The `footprint_db` argument is treated as an opaque object. For compatibility
  with early development, this module supports multiple shapes:
    - an object exposing ``get(type)->np.ndarray``
    - a dict mapping ``type -> np.ndarray``
    - a dict containing ``{"mapping": {type: np.ndarray}}``
* Footprints are assumed to be binary (0/1 or 0/255). Any non-zero is treated
  as occupied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _get_logger() -> logging.Logger:
    try:
        from ..logging import get_logger  # type: ignore

        return get_logger(__name__)
    except Exception:
        return logging.getLogger(__name__)


logger = _get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_mask_bool(mask: np.ndarray) -> np.ndarray:
    """Return a boolean mask (True = occluded)."""
    if mask is None:
        raise ValueError("mask is None")
    if not isinstance(mask, np.ndarray):
        mask = np.asarray(mask)
    if mask.ndim != 2:
        # Convert RGB/RGBA-like masks to luminance by taking any channel.
        if mask.ndim == 3 and mask.shape[2] >= 1:
            mask = mask[..., 0]
        else:
            raise ValueError("mask must be a 2D array")
    return (mask.astype(np.uint8) > 0)


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _hash_sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _stable_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_scene(scene: Dict[str, Any]) -> str:
    return _hash_sha256_bytes(_stable_json(scene))


def _hash_mask(mask_bool: np.ndarray) -> str:
    # Hash the canonical 0/1 bytes, not raw PNG.
    b = np.ascontiguousarray(mask_bool.astype(np.uint8)).tobytes()
    return _hash_sha256_bytes(b)


def _get_resolution_from_scene(scene: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Try to read (W,H) from scene.meta.resolution."""
    try:
        meta = scene.get("meta") or {}
        res = meta.get("resolution") or {}
        w = _safe_int(res.get("w"), 0)
        h = _safe_int(res.get("h"), 0)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return None


def _get_template(comp_type: str, footprint_db: Any) -> Optional[np.ndarray]:
    """Fetch the raw footprint template for a component type."""
    if not comp_type:
        return None

    # Object exposing .get(type)
    if hasattr(footprint_db, "get") and callable(getattr(footprint_db, "get")):
        try:
            tmpl = footprint_db.get(comp_type)
            if tmpl is None:
                return None
            return np.asarray(tmpl)
        except Exception:
            pass

    # Dict mapping
    if isinstance(footprint_db, dict):
        for key in (comp_type,):
            if key in footprint_db:
                try:
                    return np.asarray(footprint_db[key])
                except Exception:
                    return None

        # Common nested layouts
        for mk in ("mapping", "footprints", "templates"):
            m = footprint_db.get(mk)
            if isinstance(m, dict) and comp_type in m:
                try:
                    return np.asarray(m[comp_type])
                except Exception:
                    return None

    return None


def _canon_binary(arr: np.ndarray) -> np.ndarray:
    """Convert to uint8 binary image (0/255)."""
    if arr is None:
        raise ValueError("template is None")
    a = np.asarray(arr)
    if a.ndim != 2:
        # Pick first channel if any.
        if a.ndim == 3 and a.shape[2] >= 1:
            a = a[..., 0]
        else:
            raise ValueError("template must be 2D")
    if a.dtype != np.uint8:
        a = a.astype(np.uint8)
    a = np.where(a > 0, 255, 0).astype(np.uint8)
    return a


def _rot_to_deg(rot: float) -> float:
    """Best-effort: treat small magnitude as radians, otherwise degrees."""
    r = float(rot)
    if abs(r) <= 6.6:  # ~ 2*pi
        return float(r * (180.0 / math.pi))
    return float(r)


def _render_footprint_patch(
    *,
    node: Dict[str, Any],
    comp_type: str,
    footprint_db: Any,
) -> Tuple[np.ndarray, Tuple[int, int]]:
    """Render a node footprint into a local patch.

    Returns:
        (patch_uint8_0_255, (left, top)) where (left, top) is the top-left
        position on the full canvas.

    The patch is centered at node.pos and includes scale/rotation transforms.
    """

    tmpl = _get_template(comp_type, footprint_db)
    if tmpl is None:
        raise KeyError(f"footprint template not found for type '{comp_type}'")

    base = _canon_binary(tmpl)
    h0, w0 = int(base.shape[0]), int(base.shape[1])

    scale = _safe_float(node.get("scale", 1.0), 1.0)
    if not (scale > 0.0):
        scale = 1.0

    rot = _safe_float(node.get("rot", 0.0), 0.0)
    rot_deg = _rot_to_deg(rot)

    # Node center
    pos = node.get("pos") or {}
    cx = _safe_float(pos.get("x", 0.0), 0.0)
    cy = _safe_float(pos.get("y", 0.0), 0.0)

    try:
        from PIL import Image

        img = Image.fromarray(base, mode="L")

        # Scale
        if abs(scale - 1.0) > 1e-6:
            w1 = max(1, int(round(w0 * scale)))
            h1 = max(1, int(round(h0 * scale)))
            img = img.resize((w1, h1), resample=Image.Resampling.NEAREST)

        # Rotation (expand to keep all pixels)
        if abs(rot_deg) > 1e-6:
            img = img.rotate(rot_deg, resample=Image.Resampling.NEAREST, expand=True)

        patch = np.asarray(img, dtype=np.uint8)
        patch = np.where(patch > 0, 255, 0).astype(np.uint8)

    except Exception:
        # No PIL or failed transform: fallback to untransformed template.
        patch = base

    ph, pw = int(patch.shape[0]), int(patch.shape[1])
    left = int(round(cx - pw / 2.0))
    top = int(round(cy - ph / 2.0))
    return patch, (left, top)


def _render_transformed_patch(comp_type: str, scale: float, rot_deg: float, footprint_db: Any) -> np.ndarray:
    """Render a transformed footprint patch without placement."""
    tmpl = _get_template(comp_type, footprint_db)
    if tmpl is None:
        raise KeyError(f"footprint template not found for type '{comp_type}'")

    base = _canon_binary(tmpl)
    h0, w0 = int(base.shape[0]), int(base.shape[1])

    try:
        from PIL import Image

        img = Image.fromarray(base, mode="L")
        if abs(scale - 1.0) > 1e-6:
            w1 = max(1, int(round(w0 * scale)))
            h1 = max(1, int(round(h0 * scale)))
            img = img.resize((w1, h1), resample=Image.Resampling.NEAREST)
        if abs(rot_deg) > 1e-6:
            img = img.rotate(rot_deg, resample=Image.Resampling.NEAREST, expand=True)
        patch = np.asarray(img, dtype=np.uint8)
        return np.where(patch > 0, 255, 0).astype(np.uint8)
    except Exception:
        return base


def _occ_ratio_from_patch(mask_bool: np.ndarray, patch: np.ndarray, left: int, top: int) -> float:
    """Compute occlusion ratio between a local footprint patch and global mask."""

    h, w = int(mask_bool.shape[0]), int(mask_bool.shape[1])
    ph, pw = int(patch.shape[0]), int(patch.shape[1])

    # Intersection region on the global canvas
    x0 = max(0, left)
    y0 = max(0, top)
    x1 = min(w, left + pw)
    y1 = min(h, top + ph)
    if x1 <= x0 or y1 <= y0:
        return 0.0

    # Crop patch to overlapped area
    px0 = x0 - left
    py0 = y0 - top
    px1 = px0 + (x1 - x0)
    py1 = py0 + (y1 - y0)

    footprint_crop = (patch[py0:py1, px0:px1] > 0)
    denom = int(np.count_nonzero(footprint_crop))
    if denom <= 0:
        return 0.0

    mask_crop = mask_bool[y0:y1, x0:x1]
    inter = int(np.count_nonzero(np.logical_and(footprint_crop, mask_crop)))
    return float(inter / denom)


def _iter_nodes(scene: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    nodes = scene.get("nodes") or []
    if not isinstance(nodes, list):
        return []
    return (n for n in nodes if isinstance(n, dict))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_occlusion(scene: Dict[str, Any], mask: np.ndarray, footprint_db: Any) -> List[Dict[str, Any]]:
    """Compute per-node occlusion ratios.

    Args:
        scene: scene dict (expects scene["nodes"])
        mask: uint8/bool mask aligned to scene resolution (H,W)
        footprint_db: footprint template database

    Returns:
        List[Dict]: each item has {node_id, type, occ_ratio}
    """

    if not isinstance(scene, dict):
        raise ValueError("scene must be a dict")

    mask_bool = _ensure_mask_bool(mask)

    # Optional sanity check with scene meta resolution.
    res = _get_resolution_from_scene(scene)
    if res is not None:
        w_exp, h_exp = res
        if int(mask_bool.shape[1]) != int(w_exp) or int(mask_bool.shape[0]) != int(h_exp):
            raise ValueError(
                f"mask resolution {mask_bool.shape[1]}x{mask_bool.shape[0]} does not match scene.meta.resolution {w_exp}x{h_exp}"
            )

    out: List[Dict[str, Any]] = []
    nodes = list(_iter_nodes(scene))
    if len(nodes) == 0:
        return out

    # Lightweight fast path: empty occlusion mask means all ratios are zero.
    if not bool(mask_bool.any()):
        for node in nodes:
            out.append(
                {
                    "node_id": str(node.get("id") or ""),
                    "type": str(node.get("type") or ""),
                    "occ_ratio": 0.0,
                }
            )
        return out

    patch_cache: Dict[Tuple[str, float, float], np.ndarray] = {}
    prepared: List[Tuple[str, str, np.ndarray, int, int]] = []
    t_render_start = time.perf_counter()

    for node in nodes:
        node_id = str(node.get("id") or "")
        comp_type = str(node.get("type") or "")

        if not comp_type:
            prepared.append((node_id, comp_type, np.zeros((1, 1), dtype=np.uint8), 0, 0))
            continue

        try:
            scale = _safe_float(node.get("scale", 1.0), 1.0)
            if not (scale > 0.0):
                scale = 1.0
            rot_deg = _rot_to_deg(_safe_float(node.get("rot", 0.0), 0.0))
            key = (comp_type, round(scale, 6), round(rot_deg, 4))

            patch = patch_cache.get(key)
            if patch is None:
                patch = _render_transformed_patch(comp_type, scale, rot_deg, footprint_db)
                patch_cache[key] = patch

            pos = node.get("pos") or {}
            cx = _safe_float(pos.get("x", 0.0), 0.0)
            cy = _safe_float(pos.get("y", 0.0), 0.0)
            ph, pw = int(patch.shape[0]), int(patch.shape[1])
            left = int(round(cx - pw / 2.0))
            top = int(round(cy - ph / 2.0))
            prepared.append((node_id, comp_type, patch, left, top))
        except KeyError:
            logger.warning("footprint_missing", extra={"node_id": node_id, "type": comp_type})
            prepared.append((node_id, comp_type, np.zeros((1, 1), dtype=np.uint8), 0, 0))
        except Exception as e:
            logger.warning("occlusion_node_failed", extra={"node_id": node_id, "type": comp_type, "error": str(e)})
            prepared.append((node_id, comp_type, np.zeros((1, 1), dtype=np.uint8), 0, 0))

    render_ms = (time.perf_counter() - t_render_start) * 1000.0

    t_intersect_start = time.perf_counter()
    chunk_size = 256 if len(prepared) >= 1000 else len(prepared)
    for i in range(0, len(prepared), max(1, chunk_size)):
        chunk = prepared[i : i + max(1, chunk_size)]
        for node_id, comp_type, patch, left, top in chunk:
            occ_ratio = _occ_ratio_from_patch(mask_bool, patch, left, top)
            if not (0.0 <= occ_ratio <= 1.0) or math.isnan(occ_ratio):
                occ_ratio = float(min(1.0, max(0.0, occ_ratio))) if not math.isnan(occ_ratio) else 0.0
            out.append({"node_id": node_id, "type": comp_type, "occ_ratio": float(occ_ratio)})
    intersect_ms = (time.perf_counter() - t_intersect_start) * 1000.0

    logger.info(
        "occlusion_compute_stats",
        extra={
            "nodes": len(nodes),
            "cache_size": len(patch_cache),
            "render_ms": round(render_ms, 3),
            "intersect_ms": round(intersect_ms, 3),
        },
    )

    return out


def compute_counts(
    scene: Dict[str, Any],
    occlusion_items: List[Dict[str, Any]],
    occ_threshold: float,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Compute counts_all and counts_visible.

    A node is considered "visible" if its occlusion ratio is strictly less than
    `occ_threshold` (i.e., `occ_ratio >= occ_threshold` is excluded).
    """

    if not isinstance(scene, dict):
        raise ValueError("scene must be a dict")

    thr = float(occ_threshold)
    if not (0.0 <= thr <= 1.0):
        raise ValueError("occ_threshold must be within [0, 1]")

    # Map node_id -> occ_ratio for quick lookup.
    occ_by_id: Dict[str, float] = {}
    for it in (occlusion_items or []):
        if not isinstance(it, dict):
            continue
        nid = str(it.get("node_id") or "")
        if not nid:
            continue
        occ_by_id[nid] = float(it.get("occ_ratio") or 0.0)

    counts_all: Dict[str, int] = {}
    counts_visible: Dict[str, int] = {}

    for node in _iter_nodes(scene):
        t = str(node.get("type") or "")
        if not t:
            continue
        counts_all[t] = counts_all.get(t, 0) + 1

        nid = str(node.get("id") or "")
        occ = float(occ_by_id.get(nid, 0.0))
        if occ < thr:
            counts_visible[t] = counts_visible.get(t, 0) + 1

    return counts_all, counts_visible


def compute_label(
    scene: Dict[str, Any],
    mask: np.ndarray,
    footprint_db: Any,
    function: str,
    occ_threshold: float = 0.9,
) -> Dict[str, Any]:
    """Assemble a label dict (label.json payload).

    The router may add defaults like label_version/function/occ_threshold.
    """

    func = (function or "").strip() or "UNKNOWN"
    thr = float(occ_threshold)

    t_render_start = time.perf_counter()
    occ_items = compute_occlusion(scene=scene, mask=mask, footprint_db=footprint_db)
    render_ms = (time.perf_counter() - t_render_start) * 1000.0

    t_assemble_start = time.perf_counter()
    counts_all, counts_visible = compute_counts(scene=scene, occlusion_items=occ_items, occ_threshold=thr)
    assemble_ms = (time.perf_counter() - t_assemble_start) * 1000.0

    # Optional audit hashes.
    meta: Dict[str, Any] = {}
    try:
        meta["scene_hash"] = _hash_scene(scene)
    except Exception:
        pass
    try:
        meta["mask_hash"] = _hash_mask(_ensure_mask_bool(mask))
    except Exception:
        pass

    label_obj = {
        "label_version": "0.3",
        "counts_all": counts_all,
        "counts_visible": counts_visible,
        "occlusion": occ_items,
        "occ_threshold": thr,
        "function": func,
        "meta": meta,
    }

    logger.info(
        "label_compute_stats",
        extra={
            "render_ms": round(render_ms, 3),
            "assemble_ms": round(assemble_ms, 3),
            "nodes": len(scene.get("nodes") or []),
        },
    )
    return label_obj
