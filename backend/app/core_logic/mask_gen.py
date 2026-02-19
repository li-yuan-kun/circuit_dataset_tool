"""backend/app/core_logic/mask_gen.py

Irregular mask generation utilities.

This module implements a small plugin system ("strategies") for generating
binary occlusion masks (0/255, uint8) aligned to the scene resolution.

Design goals (v0.3+):
  - Deterministic given (scene.meta.seed, strategy, params)
  - No reliance on global RNG; everything uses an explicit numpy Generator
  - Output is a binary mask compatible with routers (/mask/generate)

Currently implemented strategies:
  - value_noise  : smooth value-noise (Perlin-like) thresholded to target ratio
  - strokes      : random-walk brush strokes until reaching a target ratio

Both strategies optionally support "focus" sampling around scene nodes.

Note:
  - The occlusion/footprint logic is handled elsewhere (core_logic/occlusion.py).
  - This module intentionally keeps dependencies light: numpy + Pillow.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, Optional, Protocol, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Strategy protocol + registry
# ---------------------------------------------------------------------------


class MaskStrategy(Protocol):
    name: str

    def generate(
        self,
        resolution: Tuple[int, int],
        scene: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Generate a binary mask.

        Returns:
            np.ndarray: uint8 array with values 0 or 255, shape (H, W)
        """


_STRATEGIES: Dict[str, MaskStrategy] = {}
_ALIASES: Dict[str, str] = {
    # noise
    "noise": "value_noise",
    "noise_blob": "value_noise",
    "perlin": "value_noise",
    "value": "value_noise",
    "value-noise": "value_noise",
    # strokes
    "random_strokes": "strokes",
    "brush": "strokes",
}


def _canon(name: str) -> str:
    n = (name or "").strip().lower()
    return _ALIASES.get(n, n)


def list_strategies() -> list[str]:
    """List registered strategy names (including canonical names only)."""
    return sorted({k for k in _STRATEGIES.keys()})


def register_strategy(strategy: MaskStrategy) -> None:
    """Register a strategy."""
    _STRATEGIES[_canon(strategy.name)] = strategy


# ---------------------------------------------------------------------------
# Seed derivation (reproducibility)
# ---------------------------------------------------------------------------


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash32(*parts: Any) -> int:
    """A stable 32-bit hash for seed derivation."""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (bytes, bytearray)):
            b = bytes(p)
        else:
            b = str(p).encode("utf-8", errors="ignore")
        h.update(b)
        h.update(b"\x1f")
    return int.from_bytes(h.digest()[:4], "little", signed=False)


def _derive_seed(base_seed: int, kind: str, strategy: str, params: Dict[str, Any]) -> int:
    """Derive a sub-seed from scene seed + (kind/strategy/params)."""
    return _hash32(int(base_seed) & 0xFFFFFFFF, kind, _canon(strategy), _stable_json(params))


# ---------------------------------------------------------------------------
# Param validation / defaults
# ---------------------------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def decode_params_and_validate(strategy: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate parameters and fill defaults.

    The router treats validation errors as 422.
    """

    s = _canon(strategy)
    if s not in _STRATEGIES:
        raise ValueError(f"Unknown mask strategy: {strategy}")

    p: Dict[str, Any] = dict(params or {})

    # Shared
    ratio = _as_float(p.get("ratio", p.get("occ_ratio", 0.2)), 0.2)
    if not (0.0 <= ratio <= 1.0):
        raise ValueError("ratio must be within [0, 1]")
    p["ratio"] = float(ratio)

    focus = _as_float(p.get("focus", 0.0), 0.0)
    p["focus"] = float(_clamp(focus, 0.0, 1.0))
    p["focus_sigma"] = int(max(1, _as_int(p.get("focus_sigma", 140), 140)))
    p["focus_jitter"] = float(max(0.0, _as_float(p.get("focus_jitter", 0.35), 0.35)))

    if s == "value_noise":
        p["base_scale"] = int(max(2, _as_int(p.get("base_scale", p.get("scale", 64)), 64)))
        p["octaves"] = int(_clamp(_as_int(p.get("octaves", 3), 3), 1, 8))
        p["lacunarity"] = float(max(1.1, _as_float(p.get("lacunarity", 2.0), 2.0)))
        p["gain"] = float(_clamp(_as_float(p.get("gain", 0.5), 0.5), 0.05, 0.99))
        p["blur"] = float(max(0.0, _as_float(p.get("blur", 0.0), 0.0)))
        p["open_radius"] = int(max(0, _as_int(p.get("open_radius", 0), 0)))
        p["close_radius"] = int(max(0, _as_int(p.get("close_radius", 0), 0)))
        p["invert"] = _as_bool(p.get("invert", False), False)
        # Whether to compute threshold by exact quantile (default) or fixed.
        p["use_quantile"] = _as_bool(p.get("use_quantile", True), True)

    elif s == "strokes":
        p["max_strokes"] = int(max(1, _as_int(p.get("max_strokes", 64), 64)))
        p["min_strokes"] = int(max(0, _as_int(p.get("min_strokes", 1), 1)))
        p["stroke_len"] = int(max(4, _as_int(p.get("stroke_len", 80), 80)))
        p["stroke_len_jitter"] = float(max(0.0, _as_float(p.get("stroke_len_jitter", 0.35), 0.35)))
        p["step"] = float(max(0.5, _as_float(p.get("step", 10.0), 10.0)))
        p["turn_sigma"] = float(max(0.0, _as_float(p.get("turn_sigma", 0.35), 0.35)))

        wmin = _as_int(p.get("width_min", p.get("width", 12)), 12)
        wmax = _as_int(p.get("width_max", max(wmin, 24)), max(wmin, 24))
        if wmin <= 0 or wmax <= 0 or wmin > wmax:
            raise ValueError("width_min/width_max invalid")
        p["width_min"] = int(wmin)
        p["width_max"] = int(wmax)
        p["close_radius"] = int(max(0, _as_int(p.get("close_radius", 0), 0)))

    else:
        # Future strategies may define their own defaults inside generate().
        pass

    return p


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _normalize_resolution(resolution: Any) -> Tuple[int, int]:
    """Return (W, H). Accepts tuple/list/dict{"w","h"}."""
    if isinstance(resolution, dict):
        w = int(resolution.get("w", 0) or 0)
        h = int(resolution.get("h", 0) or 0)
        if w <= 0 or h <= 0:
            raise ValueError("resolution.w/h must be positive")
        return w, h
    if isinstance(resolution, (tuple, list)) and len(resolution) == 2:
        w, h = int(resolution[0]), int(resolution[1])
        if w <= 0 or h <= 0:
            raise ValueError("resolution must be positive")
        return w, h
    raise ValueError("resolution must be (w,h) or {w,h}")


def _ensure_binary_uint8(mask: np.ndarray) -> np.ndarray:
    if mask is None:
        raise ValueError("mask is None")
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    # map any non-zero to 255
    mask = np.where(mask > 0, 255, 0).astype(np.uint8)
    return mask


def _scene_node_positions(scene: Optional[Dict[str, Any]]) -> np.ndarray:
    """Extract node centers as Nx2 array (x,y)."""
    if not isinstance(scene, dict):
        return np.zeros((0, 2), dtype=np.float32)
    nodes = scene.get("nodes") or []
    pts = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        pos = n.get("pos") or {}
        try:
            x = float(pos.get("x"))
            y = float(pos.get("y"))
            pts.append((x, y))
        except Exception:
            continue
    if not pts:
        return np.zeros((0, 2), dtype=np.float32)
    return np.asarray(pts, dtype=np.float32)


def _focus_map(w: int, h: int, pts_xy: np.ndarray, sigma: float) -> np.ndarray:
    """Compute a soft focus map in [0,1] around node centers."""
    if pts_xy.size == 0:
        return np.zeros((h, w), dtype=np.float32)

    # To keep it fast, render focus map on a coarse grid then upsample.
    # coarse size ~ 1/8 resolution
    cw = max(8, w // 8)
    ch = max(8, h // 8)
    xs = (np.linspace(0, w - 1, cw, dtype=np.float32))[None, :]
    ys = (np.linspace(0, h - 1, ch, dtype=np.float32))[:, None]

    sigma2 = float(max(1.0, sigma)) ** 2
    acc = np.zeros((ch, cw), dtype=np.float32)
    for (x0, y0) in pts_xy:
        dx2 = (xs - x0) ** 2
        dy2 = (ys - y0) ** 2
        acc = np.maximum(acc, np.exp(-(dx2 + dy2) / (2.0 * sigma2)))

    # Normalize already in (0,1]; upsample
    try:
        from PIL import Image

        im = Image.fromarray((acc * 255.0).astype(np.uint8), mode="L")
        im = im.resize((w, h), resample=Image.Resampling.BILINEAR)
        out = np.asarray(im, dtype=np.float32) / 255.0
        return out
    except Exception:
        # fallback: nearest
        return np.kron(acc, np.ones((h // ch + 1, w // cw + 1), dtype=np.float32))[:h, :w]


def _apply_morph(mask: np.ndarray, *, open_radius: int = 0, close_radius: int = 0) -> np.ndarray:
    """Apply simple morphology (opening/closing) on a binary mask."""
    if open_radius <= 0 and close_radius <= 0:
        return mask
    try:
        from PIL import Image, ImageFilter

        im = Image.fromarray(_ensure_binary_uint8(mask), mode="L")
        if close_radius > 0:
            k = int(close_radius) * 2 + 1
            im = im.filter(ImageFilter.MaxFilter(size=k)).filter(ImageFilter.MinFilter(size=k))
        if open_radius > 0:
            k = int(open_radius) * 2 + 1
            im = im.filter(ImageFilter.MinFilter(size=k)).filter(ImageFilter.MaxFilter(size=k))
        return _ensure_binary_uint8(np.asarray(im, dtype=np.uint8))
    except Exception:
        return _ensure_binary_uint8(mask)


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


@dataclass
class ValueNoiseStrategy:
    name: str = "value_noise"

    def generate(
        self,
        resolution: Tuple[int, int],
        scene: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        rng: np.random.Generator,
    ) -> np.ndarray:
        w, h = resolution
        ratio = float(params.get("ratio", 0.2))
        if ratio <= 0.0:
            return np.zeros((h, w), dtype=np.uint8)
        if ratio >= 1.0:
            return np.full((h, w), 255, dtype=np.uint8)

        base_scale = int(params.get("base_scale", 64))
        octaves = int(params.get("octaves", 3))
        lacunarity = float(params.get("lacunarity", 2.0))
        gain = float(params.get("gain", 0.5))
        blur = float(params.get("blur", 0.0))
        invert = bool(params.get("invert", False))
        open_radius = int(params.get("open_radius", 0))
        close_radius = int(params.get("close_radius", 0))
        use_quantile = bool(params.get("use_quantile", True))

        focus = float(params.get("focus", 0.0))
        focus_sigma = float(params.get("focus_sigma", 140))

        # Accumulate octave noise.
        field = np.zeros((h, w), dtype=np.float32)
        amp = 1.0
        amp_sum = 0.0
        cur_scale = float(base_scale)

        try:
            from PIL import Image, ImageFilter

            for _ in range(octaves):
                cell = max(2, int(round(cur_scale)))
                gx = int(np.ceil(w / cell)) + 2
                gy = int(np.ceil(h / cell)) + 2
                grid = rng.random((gy, gx), dtype=np.float32)
                im = Image.fromarray((grid * 255.0).astype(np.uint8), mode="L")
                im = im.resize((w, h), resample=Image.Resampling.BILINEAR)
                if blur > 0:
                    im = im.filter(ImageFilter.GaussianBlur(radius=float(blur)))
                layer = (np.asarray(im, dtype=np.float32) / 255.0).astype(np.float32)

                field += layer * amp
                amp_sum += amp
                amp *= gain
                cur_scale /= lacunarity

        except Exception:
            # Fallback: simple random field (no smoothing).
            field = rng.random((h, w), dtype=np.float32)
            amp_sum = 1.0

        if amp_sum > 0:
            field /= float(amp_sum)

        # Optional focus towards nodes.
        if focus > 1e-6 and isinstance(scene, dict):
            pts = _scene_node_positions(scene)
            if pts.size > 0:
                fm = _focus_map(w, h, pts, sigma=focus_sigma)
                # Bias by lifting values near nodes.
                field = np.clip(field + focus * fm, 0.0, 1.0)

        # Threshold to reach desired ratio.
        if use_quantile:
            thr = float(np.quantile(field, 1.0 - ratio))
        else:
            thr = float(1.0 - ratio)

        m = (field >= thr)
        if invert:
            m = ~m
        mask = np.where(m, 255, 0).astype(np.uint8)
        mask = _apply_morph(mask, open_radius=open_radius, close_radius=close_radius)
        return mask


@dataclass
class StrokesStrategy:
    name: str = "strokes"

    def _sample_start(
        self,
        w: int,
        h: int,
        scene: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        rng: np.random.Generator,
    ) -> Tuple[float, float]:
        focus = float(params.get("focus", 0.0))
        if focus <= 1e-6 or not isinstance(scene, dict):
            return float(rng.uniform(0, w - 1)), float(rng.uniform(0, h - 1))

        pts = _scene_node_positions(scene)
        if pts.size == 0:
            return float(rng.uniform(0, w - 1)), float(rng.uniform(0, h - 1))

        # Choose a node uniformly, then add jitter.
        idx = int(rng.integers(0, pts.shape[0]))
        x0, y0 = float(pts[idx, 0]), float(pts[idx, 1])
        sigma = float(params.get("focus_sigma", 140))
        jitter = float(params.get("focus_jitter", 0.35))
        dx = rng.normal(0.0, sigma * jitter)
        dy = rng.normal(0.0, sigma * jitter)
        x = float(np.clip(x0 + dx, 0.0, w - 1.0))
        y = float(np.clip(y0 + dy, 0.0, h - 1.0))
        return x, y

    def _random_walk(
        self,
        x0: float,
        y0: float,
        n_steps: int,
        step: float,
        turn_sigma: float,
        rng: np.random.Generator,
    ) -> list[Tuple[float, float]]:
        pts: list[Tuple[float, float]] = [(x0, y0)]
        theta = float(rng.uniform(0.0, 2.0 * np.pi))
        for _ in range(n_steps):
            theta += float(rng.normal(0.0, turn_sigma))
            x0 += float(np.cos(theta) * step)
            y0 += float(np.sin(theta) * step)
            pts.append((x0, y0))
        return pts

    def generate(
        self,
        resolution: Tuple[int, int],
        scene: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        rng: np.random.Generator,
    ) -> np.ndarray:
        w, h = resolution
        ratio_tgt = float(params.get("ratio", 0.2))
        if ratio_tgt <= 0.0:
            return np.zeros((h, w), dtype=np.uint8)
        if ratio_tgt >= 1.0:
            return np.full((h, w), 255, dtype=np.uint8)

        max_strokes = int(params.get("max_strokes", 64))
        min_strokes = int(params.get("min_strokes", 1))
        stroke_len = int(params.get("stroke_len", 80))
        stroke_len_jitter = float(params.get("stroke_len_jitter", 0.35))
        step = float(params.get("step", 10.0))
        turn_sigma = float(params.get("turn_sigma", 0.35))
        width_min = int(params.get("width_min", 12))
        width_max = int(params.get("width_max", 24))
        close_radius = int(params.get("close_radius", 0))

        try:
            from PIL import Image, ImageDraw

            img = Image.new("L", (w, h), color=0)
            draw = ImageDraw.Draw(img)

            last_ratio = 0.0
            for i in range(max_strokes):
                x0, y0 = self._sample_start(w, h, scene, params, rng)
                # jittered length
                n_steps = int(max(4, round(stroke_len * (1.0 + rng.normal(0.0, stroke_len_jitter)))))
                pts = self._random_walk(x0, y0, n_steps, step, turn_sigma, rng)
                # Clip points to image bounds (ImageDraw is fine with out-of-bounds, but this keeps it stable)
                pts_clip = [(float(np.clip(x, 0, w - 1)), float(np.clip(y, 0, h - 1))) for x, y in pts]
                width = int(rng.integers(width_min, width_max + 1))
                draw.line(pts_clip, fill=255, width=width)

                if i + 1 < min_strokes:
                    continue

                # Check coverage every few strokes to reduce overhead.
                if (i % 2) == 0 or i == max_strokes - 1:
                    arr = np.asarray(img, dtype=np.uint8)
                    last_ratio = float(np.count_nonzero(arr) / (w * h))
                    if last_ratio >= ratio_tgt:
                        break

            arr = np.asarray(img, dtype=np.uint8)
            if close_radius > 0:
                arr = _apply_morph(arr, close_radius=close_radius)
            return _ensure_binary_uint8(arr)

        except Exception:
            # Fallback: random rectangles if PIL drawing fails.
            mask = np.zeros((h, w), dtype=np.uint8)
            n = max(1, int(max_strokes))
            for _ in range(n):
                x0 = int(rng.integers(0, w))
                y0 = int(rng.integers(0, h))
                ww = int(rng.integers(width_min, width_max + 1))
                hh = int(rng.integers(width_min, width_max + 1))
                x1 = int(np.clip(x0 + ww, 0, w))
                y1 = int(np.clip(y0 + hh, 0, h))
                mask[y0:y1, x0:x1] = 255
                if float(np.count_nonzero(mask) / (w * h)) >= ratio_tgt:
                    break
            return _ensure_binary_uint8(mask)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_mask(
    strategy: str,
    resolution: Any,
    scene: Optional[Dict[str, Any]],
    params: Dict[str, Any],
    seed: int,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Generate a mask and return (mask_np, meta).

    Args:
        strategy: strategy name (aliases accepted)
        resolution: (w,h) or {w,h}
        scene: scene dict (optional)
        params: validated params
        seed: base seed from scene.meta.seed
    """

    s = _canon(strategy)
    if s not in _STRATEGIES:
        raise ValueError(f"Unknown mask strategy: {strategy}")

    w, h = _normalize_resolution(resolution)
    params2 = decode_params_and_validate(s, params)
    seed_used = _derive_seed(seed, "mask", s, params2)
    rng = np.random.default_rng(seed_used)

    mask = _STRATEGIES[s].generate((w, h), scene, params2, rng)
    mask = _ensure_binary_uint8(mask)

    achieved = float(np.count_nonzero(mask) / (w * h)) if (w > 0 and h > 0) else 0.0

    meta = {
        "seed_base": int(seed),
        "seed_used": int(seed_used),
        "strategy": s,
        "params": params2,
        "ratio_achieved": achieved,
    }
    return mask, meta


def encode_png(mask: np.ndarray) -> bytes:
    """Encode mask (0/255 uint8) into PNG bytes."""
    from PIL import Image

    m = _ensure_binary_uint8(mask)
    img = Image.fromarray(m, mode="L")
    bio = BytesIO()
    img.save(bio, format="PNG", optimize=True)
    return bio.getvalue()


# ---------------------------------------------------------------------------
# Register built-in strategies
# ---------------------------------------------------------------------------


register_strategy(ValueNoiseStrategy())
register_strategy(StrokesStrategy())