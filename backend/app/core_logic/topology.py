"""backend/app/core_logic/topology.py

Topology-preserving layout shuffle (v0.3+).

This module implements the core algorithm behind the HTTP endpoint
`POST /topology/shuffle`.

High-level behavior
-------------------
1) Randomize node placement (non-overlapping bounding boxes) within scene
   resolution.
2) Keep the netlist intact (node/pin endpoints unchanged).
3) Optionally regenerate simple polyline paths for each net.

Implementation notes
--------------------
- Pure logic: no FastAPI dependency.
- Deterministic: all randomness is derived from the given `seed` and `params`
  via a stable hash.
- Vocab-driven sizes/pins are *best-effort*. If the vocab does not provide
  pin coordinates, routing uses node centers.

The router will call:
    scene_shuffled, meta = shuffle_scene(...)
    verify_topology_invariant(scene, scene_shuffled)

"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
# Stable seed derivation
# ---------------------------------------------------------------------------


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash32(*parts: Any) -> int:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (bytes, bytearray)):
            b = bytes(p)
        else:
            b = str(p).encode("utf-8", errors="ignore")
        h.update(b)
        h.update(b"\x1f")
    return int.from_bytes(h.digest()[:4], "little", signed=False)


def _derive_seed(base_seed: int, kind: str, params: Dict[str, Any]) -> int:
    # Keep it stable across runs.
    return _hash32(int(base_seed) & 0xFFFFFFFF, kind, _stable_json(params or {}))


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _get_resolution(scene: Dict[str, Any], default: Tuple[int, int] = (1024, 1024)) -> Tuple[int, int]:
    try:
        meta = scene.get("meta") or {}
        res = meta.get("resolution") or {}
        w = _safe_int(res.get("w"), default[0])
        h = _safe_int(res.get("h"), default[1])
        if w <= 0 or h <= 0:
            return default
        return (w, h)
    except Exception:
        return default


def _iter_nodes(scene: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    nodes = scene.get("nodes") or []
    if not isinstance(nodes, list):
        return []
    return (n for n in nodes if isinstance(n, dict))


def _iter_nets(scene: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    nets = scene.get("nets") or []
    if not isinstance(nets, list):
        return []
    return (e for e in nets if isinstance(e, dict))


def _canon_type_size(comp_type: str, vocab: Dict[str, Any], default: Tuple[float, float] = (80.0, 80.0)) -> Tuple[float, float]:
    """Return (w, h) for a component type from vocab.

    Expected vocab layout (best-effort):
      vocab["types"][type]["size"] -> {"w":..., "h":...}
    """

    if not comp_type or not isinstance(vocab, dict):
        return default
    types = vocab.get("types") or {}
    if not isinstance(types, dict):
        return default

    t = types.get(comp_type)
    if not isinstance(t, dict):
        return default

    size = t.get("size")
    if isinstance(size, dict):
        w = _safe_float(size.get("w"), default[0])
        h = _safe_float(size.get("h"), default[1])
        if w > 0 and h > 0:
            return (w, h)
    # fallback keys
    w = _safe_float(t.get("w"), default[0])
    h = _safe_float(t.get("h"), default[1])
    if w > 0 and h > 0:
        return (w, h)
    return default


def _node_bbox(node: Dict[str, Any], vocab: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Axis-aligned bbox for collision detection (approx; ignores rotation)."""

    comp_type = str(node.get("type") or "")
    w0, h0 = _canon_type_size(comp_type, vocab)
    scale = _safe_float(node.get("scale", 1.0), 1.0)
    if not (scale > 0.0):
        scale = 1.0
    w = float(w0) * float(scale)
    h = float(h0) * float(scale)

    pos = node.get("pos") or {}
    x = _safe_float(pos.get("x", 0.0), 0.0)
    y = _safe_float(pos.get("y", 0.0), 0.0)

    return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)


def _bbox_intersect(b1: Tuple[float, float, float, float], b2: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = b1
    bx0, by0, bx1, by1 = b2
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _expand_bbox(b: Tuple[float, float, float, float], pad: float) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = b
    p = float(max(0.0, pad))
    return (x0 - p, y0 - p, x1 + p, y1 + p)


# ---------------------------------------------------------------------------
# Pin coordinate helpers (best-effort)
# ---------------------------------------------------------------------------


def _rot_to_rad(rot: float) -> float:
    """Best-effort: treat small magnitude as radians, otherwise degrees."""

    r = float(rot)
    if abs(r) <= 6.6:  # ~2*pi
        return r
    return float(r * (math.pi / 180.0))


def _get_pin_offset_local(comp_type: str, pin: str, vocab: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Return pin offset (dx,dy) in *component local coordinates*.

    Supports multiple vocab shapes:
      - pins: [{name:"p0", x:..., y:...}, ...]
      - pins: {"p0": {x:...,y:...}, ...}
      - pins: [{"id"/"pin"/...}]  (best-effort)

    If coordinates are missing, returns None.
    """

    try:
        types = vocab.get("types") or {}
        t = types.get(comp_type) or {}
        pins = t.get("pins")

        if isinstance(pins, dict):
            p = pins.get(pin)
            if isinstance(p, dict) and ("x" in p) and ("y" in p):
                return (_safe_float(p.get("x"), 0.0), _safe_float(p.get("y"), 0.0))

        if isinstance(pins, list):
            for it in pins:
                if not isinstance(it, dict):
                    continue
                name = it.get("name") or it.get("id") or it.get("pin")
                if str(name) != str(pin):
                    continue
                if "x" in it and "y" in it:
                    return (_safe_float(it.get("x"), 0.0), _safe_float(it.get("y"), 0.0))

    except Exception:
        return None

    return None


def _endpoint_xy(scene: Dict[str, Any], ep: Dict[str, Any], vocab: Dict[str, Any]) -> Tuple[float, float]:
    """Compute absolute endpoint coordinate for a net endpoint dict.

    Endpoint format: {"node": "n1", "pin": "p0"}

    If vocab lacks pin coords, falls back to node center.
    Applies node scale and rotation to pin offset if possible.
    """

    node_id = str(ep.get("node") or "")
    pin_name = str(ep.get("pin") or "")

    node = None
    for n in _iter_nodes(scene):
        if str(n.get("id") or "") == node_id:
            node = n
            break

    if not isinstance(node, dict):
        return (0.0, 0.0)

    pos = node.get("pos") or {}
    cx = _safe_float(pos.get("x", 0.0), 0.0)
    cy = _safe_float(pos.get("y", 0.0), 0.0)

    comp_type = str(node.get("type") or "")
    off = _get_pin_offset_local(comp_type, pin_name, vocab)
    if off is None:
        return (cx, cy)

    dx, dy = off
    scale = _safe_float(node.get("scale", 1.0), 1.0)
    if not (scale > 0.0):
        scale = 1.0
    dx *= scale
    dy *= scale

    rot = _rot_to_rad(_safe_float(node.get("rot", 0.0), 0.0))
    if abs(rot) > 1e-9:
        c = float(math.cos(rot))
        s = float(math.sin(rot))
        dx2 = dx * c - dy * s
        dy2 = dx * s + dy * c
        dx, dy = dx2, dy2

    return (cx + dx, cy + dy)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_topology_invariant(scene_a: Dict[str, Any], scene_b: Dict[str, Any]) -> None:
    """Verify that two scenes have identical net endpoint sets.

    Raises:
        ValueError: if invariant is broken.

    Invariant definition:
      The multiset of undirected endpoint pairs must match:
        { ((node,pin),(node,pin)), ... }

    Node positions/paths/meta are ignored.
    """

    if not isinstance(scene_a, dict) or not isinstance(scene_b, dict):
        raise ValueError("topology invariant check expects dict scenes")

    def _edge_key(net: Dict[str, Any]) -> Optional[Tuple[Tuple[str, str], Tuple[str, str]]]:
        fr = net.get("from") or net.get("from_")
        to = net.get("to")
        if not isinstance(fr, dict) or not isinstance(to, dict):
            return None
        a = (str(fr.get("node") or ""), str(fr.get("pin") or ""))
        b = (str(to.get("node") or ""), str(to.get("pin") or ""))
        if not a[0] or not a[1] or not b[0] or not b[1]:
            return None
        return tuple(sorted([a, b]))  # undirected

    edges_a: List[Tuple[Tuple[str, str], Tuple[str, str]]] = []
    edges_b: List[Tuple[Tuple[str, str], Tuple[str, str]]] = []

    for e in _iter_nets(scene_a):
        k = _edge_key(e)
        if k is not None:
            edges_a.append(k)
    for e in _iter_nets(scene_b):
        k = _edge_key(e)
        if k is not None:
            edges_b.append(k)

    edges_a.sort()
    edges_b.sort()

    if edges_a != edges_b:
        # Provide a short diff hint.
        set_a = set(edges_a)
        set_b = set(edges_b)
        missing = list(sorted(set_a - set_b))[:5]
        extra = list(sorted(set_b - set_a))[:5]
        raise ValueError(
            "Topology invariant violated: endpoint pairs differ. "
            f"missing={missing} extra={extra}"
        )


def place_nodes_random_nonoverlap(
    nodes: List[Dict[str, Any]],
    vocab: Dict[str, Any],
    resolution: Tuple[int, int],
    margin: int,
    max_tries: int,
    rng: np.random.Generator,
) -> List[Dict[str, Any]]:
    """Randomly place nodes within canvas bounds without bbox overlaps.

    Args:
        nodes: list of node dicts (modified in-place)
        vocab: vocab dict (sizes)
        resolution: (W,H)
        margin: padding from canvas border (pixels)
        max_tries: attempts per node
        rng: numpy random generator

    Returns:
        nodes: the same list with updated node.pos.{x,y}

    Notes:
        - Collision check uses axis-aligned bbox, ignoring rotation.
        - If placement fails for some node, it will be placed anyway with
          possible overlap, and a warning will be logged.
    """

    w, h = int(resolution[0]), int(resolution[1])
    pad = float(max(0, int(margin)))

    # Work on a shallow copy for order shuffling; nodes themselves are dicts.
    order = list(range(len(nodes)))
    rng.shuffle(order)

    placed_bboxes: List[Tuple[float, float, float, float]] = []
    failures = 0

    for idx in order:
        n = nodes[idx]
        if not isinstance(n, dict):
            continue

        comp_type = str(n.get("type") or "")
        base_w, base_h = _canon_type_size(comp_type, vocab)
        scale = _safe_float(n.get("scale", 1.0), 1.0)
        if not (scale > 0.0):
            scale = 1.0
        bw = float(base_w) * float(scale)
        bh = float(base_h) * float(scale)

        # Sample center coordinates with boundary constraints.
        x_lo = pad + bw / 2.0
        x_hi = float(w) - pad - bw / 2.0
        y_lo = pad + bh / 2.0
        y_hi = float(h) - pad - bh / 2.0

        # If the canvas is too small, relax bounds.
        if x_hi <= x_lo:
            x_lo, x_hi = bw / 2.0, float(w) - bw / 2.0
        if y_hi <= y_lo:
            y_lo, y_hi = bh / 2.0, float(h) - bh / 2.0

        # If still impossible, clamp to center.
        if x_hi <= x_lo:
            x_lo = x_hi = float(w) / 2.0
        if y_hi <= y_lo:
            y_lo = y_hi = float(h) / 2.0

        placed = False
        last_bbox = None

        for _ in range(int(max_tries)):
            cx = float(rng.uniform(x_lo, x_hi))
            cy = float(rng.uniform(y_lo, y_hi))
            n.setdefault("pos", {})
            n["pos"]["x"] = cx
            n["pos"]["y"] = cy

            bb = _expand_bbox(_node_bbox(n, vocab), pad=0.0)
            last_bbox = bb

            ok = True
            for bb2 in placed_bboxes:
                if _bbox_intersect(bb, bb2):
                    ok = False
                    break
            if ok:
                placed = True
                placed_bboxes.append(bb)
                break

        if not placed:
            failures += 1
            # Use last sampled bbox if any; otherwise force center.
            if last_bbox is None:
                n.setdefault("pos", {})
                n["pos"]["x"] = float(w) / 2.0
                n["pos"]["y"] = float(h) / 2.0
                last_bbox = _node_bbox(n, vocab)
            placed_bboxes.append(last_bbox)

    if failures > 0:
        logger.warning("node_placement_failed", extra={"failures": failures, "num_nodes": len(nodes)})

    return nodes


def route_net_two_seg(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    bend_mode: str = "hv",
) -> List[Dict[str, float]]:
    """Return a 3-point polyline path: start -> bend -> end."""

    x0, y0 = float(p0[0]), float(p0[1])
    x1, y1 = float(p1[0]), float(p1[1])

    bm = (bend_mode or "hv").strip().lower()
    if bm not in ("hv", "vh"):
        bm = "hv"

    if bm == "hv":
        mid = (x1, y0)
    else:
        mid = (x0, y1)

    return [{"x": x0, "y": y0}, {"x": float(mid[0]), "y": float(mid[1])}, {"x": x1, "y": y1}]


def route_all_nets(
    scene: Dict[str, Any],
    vocab: Dict[str, Any],
    mode: str = "two_seg",
    *,
    bend_mode: str = "hv",
    rng: Optional[np.random.Generator] = None,
) -> Dict[str, Any]:
    """Generate net paths for all nets and write them back into the scene.

    Args:
        scene: scene dict
        vocab: vocab dict (optional pin coords)
        mode: "two_seg" or "straight"
        bend_mode: "hv"/"vh"/"auto" (auto chooses per net)
        rng: optional RNG for auto bend selection
    """

    m = (mode or "two_seg").strip().lower()
    bm = (bend_mode or "hv").strip().lower()

    for net in _iter_nets(scene):
        fr = net.get("from") or net.get("from_")
        to = net.get("to")
        if not isinstance(fr, dict) or not isinstance(to, dict):
            continue

        p0 = _endpoint_xy(scene, fr, vocab)
        p1 = _endpoint_xy(scene, to, vocab)

        if m == "straight":
            net["path"] = [{"x": float(p0[0]), "y": float(p0[1])}, {"x": float(p1[0]), "y": float(p1[1])}]
            continue

        # two-segment
        if bm == "auto":
            # Prefer deterministic selection if rng provided.
            if rng is None:
                choice = "hv"
            else:
                choice = "hv" if bool(rng.integers(0, 2)) else "vh"
        else:
            choice = bm

        net["path"] = route_net_two_seg(p0, p1, bend_mode=choice)

    return scene


def shuffle_scene(
    scene: Dict[str, Any],
    vocab: Dict[str, Any],
    params: Dict[str, Any],
    seed: int,
    return_paths: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Shuffle node positions while preserving netlist connectivity.

    Args:
        scene: scene dict
        vocab: vocab dict
        params: algorithm parameters
        seed: base seed (typically scene.meta.seed)
        return_paths: if True, regenerate net["path"]

    Returns:
        (scene_shuffled, meta)
    """

    if not isinstance(scene, dict):
        raise ValueError("scene must be a dict")
    if not isinstance(vocab, dict):
        raise ValueError("vocab must be a dict")

    p: Dict[str, Any] = dict(params or {})

    # Defaults (v0.3-ish).
    margin = _safe_int(p.get("margin", 20), 20)
    max_tries = _safe_int(p.get("max_tries", 2000), 2000)
    placement = str(p.get("placement", p.get("placement_mode", "random_nonoverlap")) or "random_nonoverlap")
    route_mode = str(p.get("route_mode", p.get("route", "two_seg")) or "two_seg")
    bend_mode = str(p.get("bend_mode", "auto"))

    # Resolution can be overridden via params, else scene.meta.resolution.
    res = _get_resolution(scene)
    if isinstance(p.get("resolution"), dict):
        rr = p.get("resolution") or {}
        res = (_safe_int(rr.get("w"), res[0]), _safe_int(rr.get("h"), res[1]))

    seed_used = _derive_seed(seed, "shuffle", {"placement": placement, "route_mode": route_mode, "bend_mode": bend_mode, **p})
    rng = np.random.default_rng(int(seed_used))

    scene_out = copy.deepcopy(scene)

    nodes = list(_iter_nodes(scene_out))

    if placement.strip().lower() in ("random_nonoverlap", "nonoverlap", "random"):
        place_nodes_random_nonoverlap(nodes, vocab=vocab, resolution=res, margin=margin, max_tries=max_tries, rng=rng)
    else:
        # Unknown placement strategy: fallback to non-overlap.
        logger.warning("unknown_placement_strategy", extra={"placement": placement})
        place_nodes_random_nonoverlap(nodes, vocab=vocab, resolution=res, margin=margin, max_tries=max_tries, rng=rng)

    # Write back nodes list to preserve all fields (copy.deepcopy created new list).
    # Our `nodes` list contains the same dict objects as in scene_out["nodes"],
    # so positions are already updated.

    if return_paths:
        route_all_nets(scene_out, vocab=vocab, mode=route_mode, bend_mode=bend_mode, rng=rng)
    else:
        # Explicitly clear paths to keep output compact.
        for net in _iter_nets(scene_out):
            net["path"] = []

    # Optionally persist shuffle params into scene meta for reproducibility.
    meta_scene = scene_out.setdefault("meta", {})
    params_scene = meta_scene.setdefault("params", {})
    if isinstance(params_scene, dict):
        params_scene.setdefault("shuffle", {})
        if isinstance(params_scene.get("shuffle"), dict):
            params_scene["shuffle"].update(
                {
                    "placement": placement,
                    "route_mode": route_mode,
                    "bend_mode": bend_mode,
                    "margin": margin,
                    "max_tries": max_tries,
                }
            )

    meta = {
        "seed_base": int(seed),
        "seed_used": int(seed_used),
        "resolution": {"w": int(res[0]), "h": int(res[1])},
        "placement": placement,
        "route_mode": route_mode,
        "bend_mode": bend_mode,
        "return_paths": bool(return_paths),
        "num_nodes": int(len(list(_iter_nodes(scene_out)))),
        "num_nets": int(len(list(_iter_nets(scene_out)))),
        "params": p,
    }

    return scene_out, meta
