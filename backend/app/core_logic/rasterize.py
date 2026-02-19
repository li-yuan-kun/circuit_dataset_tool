"""backend/app/core_logic/rasterize.py

Rasterization helpers for footprint-based occlusion and layout utilities.

This module supports the pixel-based occlusion workflow described in the design
spec:

- `load_vocab()` reads `shared/vocab.json`.
- `load_footprints()` loads per-component footprint raster templates from
  `shared/footprints/`.
- `render_footprint_on_canvas()` projects a node's footprint onto a full canvas.
- `bitwise_occ_ratio()` computes occlusion ratio by pixel intersection.

Compatibility
-------------
`core_logic/occlusion.py` treats `footprint_db` as an opaque object and supports
either:
- an object exposing `.get(type)->np.ndarray`, or
- a dict mapping `type -> np.ndarray`.

This module provides a simple `FootprintDB` wrapper that satisfies `.get()`.

Footprint templates
-------------------
A footprint template is a 2D binary image describing the occupied pixels of a
component at its canonical orientation/size.

- Internally, templates are stored as uint8 with values {0, 255}.
- `render_footprint_on_canvas()` returns a binary canvas in {0, 1}.

Transform conventions
---------------------
A node may specify:
- pos: {x, y}      (canvas coordinates, origin at top-left)
- scale: float     (uniform scale, default 1.0)
- rot: float       (radians or degrees; best-effort auto-detection)

Rotation auto-detection:
- If |rot| <= ~2*pi, interpret as radians.
- Otherwise interpret as degrees.

All operations are best-effort and degrade gracefully in early development.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, Union

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
# Data structures
# ---------------------------------------------------------------------------


class FootprintDB:
    """Thin wrapper around a {type -> footprint template} mapping."""

    def __init__(
        self,
        mapping: Dict[str, np.ndarray],
        canonical_sizes: Dict[str, Tuple[int, int]],
        *,
        meta: Optional[Dict[str, Any]] = None,
    ):
        self._m: Dict[str, np.ndarray] = dict(mapping or {})
        self._s: Dict[str, Tuple[int, int]] = dict(canonical_sizes or {})
        self.meta: Dict[str, Any] = dict(meta or {})

    def get(self, comp_type: str) -> np.ndarray:
        """Return the raw footprint template for `comp_type` (uint8 {0,255})."""
        return self._m[str(comp_type)]

    def canonical_size(self, comp_type: str) -> Tuple[int, int]:
        """Return (w, h) of the canonical footprint raster."""
        return self._s[str(comp_type)]

    def __contains__(self, comp_type: object) -> bool:  # pragma: no cover
        try:
            return str(comp_type) in self._m
        except Exception:
            return False

    def types(self) -> list[str]:  # pragma: no cover
        return sorted(self._m.keys())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_path(p: Union[str, Path]) -> Path:
    return p if isinstance(p, Path) else Path(str(p)).expanduser()


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


def _rot_to_deg(rot: float) -> float:
    """Best-effort: treat small magnitude as radians, otherwise degrees."""
    r = float(rot)
    if abs(r) <= 6.6:  # ~2*pi
        return float(r * (180.0 / math.pi))
    return float(r)


def _normalize_resolution(resolution: Any, default: Tuple[int, int] = (1024, 1024)) -> Tuple[int, int]:
    """Normalize resolution into (w, h)."""
    if isinstance(resolution, (tuple, list)) and len(resolution) >= 2:
        w = _safe_int(resolution[0], default[0])
        h = _safe_int(resolution[1], default[1])
        return (max(1, w), max(1, h))
    if isinstance(resolution, dict):
        w = _safe_int(resolution.get("w"), default[0])
        h = _safe_int(resolution.get("h"), default[1])
        return (max(1, w), max(1, h))
    return (max(1, int(default[0])), max(1, int(default[1])))


def _ensure_2d(arr: Any) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 2:
        return a
    if a.ndim == 3 and a.shape[2] >= 1:
        return a[..., 0]
    raise ValueError("footprint template must be a 2D array (or HxWxC)")


def _canon_binary_u8(arr: Any) -> np.ndarray:
    """Convert to uint8 binary {0,255}."""
    a = _ensure_2d(arr)
    if a.dtype != np.uint8:
        a = a.astype(np.uint8)
    return np.where(a > 0, 255, 0).astype(np.uint8)


def _extract_footprint_hint(type_def: Any) -> Optional[str]:
    """Try to extract a footprint filename/path hint from a type definition."""
    if not isinstance(type_def, dict):
        return None

    for k in ("footprint", "footprint_path", "footprintRaster", "footprint_raster"):
        v = type_def.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("path", "file", "filename", "name"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()

    return None


def _candidate_basenames(comp_type: str) -> list[str]:
    """Generate reasonable filename basenames (no extension)."""
    t = (comp_type or "").strip()
    if not t:
        return []

    out: list[str] = [t, t.lower(), t.upper()]

    s = t.replace(" ", "_")
    if s != t:
        out.extend([s, s.lower(), s.upper()])

    s2 = t.replace("/", "_")
    if s2 != t and s2 not in out:
        out.extend([s2, s2.lower(), s2.upper()])

    # Deduplicate while keeping order
    uniq: list[str] = []
    seen = set()
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def _find_first_existing(base_dir: Path, rel_or_name: str, exts: Tuple[str, ...]) -> Optional[Path]:
    """Find the first existing file either by explicit rel path or by name+ext."""
    s = (rel_or_name or "").strip()
    if not s:
        return None

    # Explicit path (may include extension)
    p = (base_dir / s) if not Path(s).is_absolute() else Path(s)
    if p.exists() and p.is_file():
        return p

    # If s has no extension, try with extensions
    if Path(s).suffix == "":
        for ext in exts:
            p2 = base_dir / f"{s}{ext}"
            if p2.exists() and p2.is_file():
                return p2

    return None


def _load_footprint_file(path: Path) -> np.ndarray:
    """Load a footprint raster from file into uint8 {0,255} 2D array."""
    suf = path.suffix.lower()

    if suf == ".npy":
        arr = np.load(str(path), allow_pickle=False)
        return _canon_binary_u8(arr)

    if suf == ".npz":
        z = np.load(str(path), allow_pickle=False)
        try:
            if len(z.files) == 0:
                raise ValueError("empty npz")
            arr = z[z.files[0]]
        finally:
            try:
                z.close()
            except Exception:
                pass
        return _canon_binary_u8(arr)

    # Image formats: PNG/JPG/etc.
    try:
        from PIL import Image
    except Exception as e:
        raise RuntimeError("Pillow is required to load image footprints") from e

    with Image.open(str(path)) as im:
        if im.mode not in ("L", "1"):
            im = im.convert("L")
        arr = np.asarray(im, dtype=np.uint8)
    return _canon_binary_u8(arr)


def _iter_vocab_types(vocab: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (type, type_def) pairs from vocab with best-effort compatibility."""
    if not isinstance(vocab, dict):
        return []
    types = vocab.get("types")
    if isinstance(types, dict):
        for k, v in types.items():
            if isinstance(k, str) and isinstance(v, dict):
                yield k, v
    elif isinstance(types, list):
        for it in types:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("type") or it.get("id")
            if isinstance(name, str) and name.strip():
                yield name.strip(), it


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_vocab(vocab_path: Union[str, Path]) -> Dict[str, Any]:
    """Load vocab.json.

    Args:
        vocab_path: path to vocab.json

    Returns:
        Parsed dict. A convenience field `vocab_path` is added.
    """
    p = _as_path(vocab_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"vocab.json not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        vocab = json.load(f)

    if not isinstance(vocab, dict):
        raise ValueError("vocab.json must be a JSON object")

    vocab.setdefault("types", {})
    vocab["vocab_path"] = str(p)
    return vocab


def load_footprints(footprint_dir: Union[str, Path], vocab: Dict[str, Any]) -> FootprintDB:
    """Load footprint raster templates.

    Lookup strategy per type:
    1) If vocab.types[type].footprint (or similar) specifies a file/path, use it.
    2) Otherwise, try `<type>.(png|jpg|jpeg|bmp|tif|tiff|npy|npz)` inside `footprint_dir`,
       with a few case/sanitized variants.

    Missing templates are allowed (warned) to keep early development unblocked.
    """
    d = _as_path(footprint_dir)
    if not d.exists() or not d.is_dir():
        logger.warning("footprint_dir_missing", extra={"footprint_dir": str(d)})
        return FootprintDB(mapping={}, canonical_sizes={}, meta={"footprint_dir": str(d), "loaded": 0})

    exts = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".npy", ".npz")

    mapping: Dict[str, np.ndarray] = {}
    sizes: Dict[str, Tuple[int, int]] = {}
    missing: list[str] = []

    for comp_type, tdef in _iter_vocab_types(vocab):
        hint = _extract_footprint_hint(tdef)
        found: Optional[Path] = None

        if hint:
            found = _find_first_existing(d, hint, exts)

        if found is None:
            for base in _candidate_basenames(comp_type):
                found = _find_first_existing(d, base, exts)
                if found is not None:
                    break

        if found is None:
            missing.append(comp_type)
            continue

        try:
            arr = _load_footprint_file(found)
            mapping[comp_type] = arr
            sizes[comp_type] = (int(arr.shape[1]), int(arr.shape[0]))  # (w,h)
        except Exception as e:
            logger.warning(
                "footprint_load_failed",
                extra={"type": comp_type, "path": str(found), "error": str(e)},
            )
            missing.append(comp_type)

    if missing:
        logger.warning(
            "footprint_missing",
            extra={"missing_types": missing[:50], "missing_count": len(missing), "loaded": len(mapping)},
        )

    return FootprintDB(
        mapping=mapping,
        canonical_sizes=sizes,
        meta={"footprint_dir": str(d), "loaded": int(len(mapping)), "missing": int(len(missing))},
    )


def render_footprint_on_canvas(
    node: Dict[str, Any],
    footprint_db: FootprintDB,
    resolution: Union[Tuple[int, int], Dict[str, Any]],
) -> np.ndarray:
    """Project a node footprint to a full canvas.

    Args:
        node: scene node dict (expects type/pos/scale/rot)
        footprint_db: loaded footprint templates
        resolution: (w,h) tuple or {"w":..,"h":..}

    Returns:
        canvas: np.uint8 array with shape (H,W) and values {0,1}
    """
    if not isinstance(node, dict):
        raise ValueError("node must be a dict")

    comp_type = str(node.get("type") or "")
    if not comp_type:
        raise ValueError("node.type is required")

    w, h = _normalize_resolution(resolution)
    canvas = np.zeros((h, w), dtype=np.uint8)

    tmpl = footprint_db.get(comp_type)
    base = _canon_binary_u8(tmpl)  # {0,255}

    scale = _safe_float(node.get("scale", 1.0), 1.0)
    if not (scale > 0.0):
        scale = 1.0

    rot = _safe_float(node.get("rot", 0.0), 0.0)
    rot_deg = _rot_to_deg(rot)

    pos = node.get("pos") or {}
    cx = _safe_float(pos.get("x", 0.0), 0.0)
    cy = _safe_float(pos.get("y", 0.0), 0.0)

    # Transform base template into a patch (0/1) using PIL if available.
    patch01: np.ndarray
    try:
        from PIL import Image

        img = Image.fromarray(base, mode="L")
        h0, w0 = int(base.shape[0]), int(base.shape[1])

        if abs(scale - 1.0) > 1e-6:
            w1 = max(1, int(round(w0 * scale)))
            h1 = max(1, int(round(h0 * scale)))
            img = img.resize((w1, h1), resample=Image.Resampling.NEAREST)

        if abs(rot_deg) > 1e-6:
            img = img.rotate(rot_deg, resample=Image.Resampling.NEAREST, expand=True)

        patch = np.asarray(img, dtype=np.uint8)
        patch01 = (patch > 0).astype(np.uint8)
    except Exception:
        # Fallback: no transform
        patch01 = (base > 0).astype(np.uint8)

    ph, pw = int(patch01.shape[0]), int(patch01.shape[1])
    left = int(round(cx - pw / 2.0))
    top = int(round(cy - ph / 2.0))

    # Compute overlap region
    x0 = max(0, left)
    y0 = max(0, top)
    x1 = min(w, left + pw)
    y1 = min(h, top + ph)
    if x1 <= x0 or y1 <= y0:
        return canvas

    # Crop patch to the overlap region
    px0 = x0 - left
    py0 = y0 - top
    px1 = px0 + (x1 - x0)
    py1 = py0 + (y1 - y0)

    patch_crop = patch01[py0:py1, px0:px1]
    canvas[y0:y1, x0:x1] = np.maximum(canvas[y0:y1, x0:x1], patch_crop.astype(np.uint8))
    return canvas


def bitwise_occ_ratio(footprint_canvas: np.ndarray, mask: np.ndarray) -> float:
    """Compute occlusion ratio by pixel intersection.

    occ_ratio = (footprint & mask) / footprint
    """
    fc = np.asarray(footprint_canvas)
    if fc.ndim != 2:
        raise ValueError("footprint_canvas must be 2D")
    denom = int(np.count_nonzero(fc))
    if denom == 0:
        return 0.0

    m = np.asarray(mask)
    if m.ndim != 2:
        if m.ndim == 3 and m.shape[2] >= 1:
            m = m[..., 0]
        else:
            raise ValueError("mask must be 2D")

    inter = int(np.count_nonzero((fc > 0) & (m > 0)))
    return float(inter / denom)


def node_bbox(node: Dict[str, Any], vocab: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """(xmin,ymin,xmax,ymax) coarse bbox (ignores rotation)."""
    if not isinstance(node, dict):
        raise ValueError("node must be a dict")
    if not isinstance(vocab, dict):
        raise ValueError("vocab must be a dict")

    t = str(node.get("type") or "")
    if not t:
        raise ValueError("node.type is required")

    types = vocab.get("types") or {}
    tdef = types.get(t) if isinstance(types, dict) else None
    if not isinstance(tdef, dict):
        raise KeyError(f"unknown component type '{t}'")

    size = tdef.get("size") or {}
    if isinstance(size, dict):
        w0 = _safe_float(size.get("w", 0.0), 0.0)
        h0 = _safe_float(size.get("h", 0.0), 0.0)
    else:
        w0 = _safe_float(tdef.get("w", 0.0), 0.0)
        h0 = _safe_float(tdef.get("h", 0.0), 0.0)

    if not (w0 > 0.0 and h0 > 0.0):
        w0, h0 = 80.0, 80.0

    scale = _safe_float(node.get("scale", 1.0), 1.0)
    if not (scale > 0.0):
        scale = 1.0

    w = float(w0) * float(scale)
    h = float(h0) * float(scale)

    pos = node.get("pos") or {}
    x = _safe_float(pos.get("x", 0.0), 0.0)
    y = _safe_float(pos.get("y", 0.0), 0.0)

    return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)


def bbox_intersect(b1: Tuple[float, float, float, float], b2: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = b1
    bx0, by0, bx1, by1 = b2
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)
