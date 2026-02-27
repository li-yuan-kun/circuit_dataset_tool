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
import heapq
import hashlib
import json
import logging
import math
import time
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
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Randomly place nodes within canvas bounds without bbox overlaps.

    Args:
        nodes: list of node dicts (modified in-place)
        vocab: vocab dict (sizes)
        resolution: (W,H)
        margin: padding from canvas border (pixels)
        max_tries: attempts per node
        rng: numpy random generator

    Returns:
        (nodes, stats): the same list with updated node.pos.{x,y} and
        placement diagnostics

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

    t_start = time.perf_counter()

    placed_bboxes: List[Tuple[float, float, float, float]] = []
    spatial_cells: Dict[Tuple[int, int], List[int]] = {}

    size_samples: List[float] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        comp_type = str(n.get("type") or "")
        base_w, base_h = _canon_type_size(comp_type, vocab)
        scale = _safe_float(n.get("scale", 1.0), 1.0)
        if not (scale > 0.0):
            scale = 1.0
        size_samples.append(max(float(base_w) * float(scale), float(base_h) * float(scale)))

    bucket_size = max(32.0, float(np.median(size_samples) if size_samples else 80.0))

    def _bbox_to_cells(bb: Tuple[float, float, float, float]) -> List[Tuple[int, int]]:
        x0, y0, x1, y1 = bb
        gx0 = int(math.floor(x0 / bucket_size))
        gy0 = int(math.floor(y0 / bucket_size))
        gx1 = int(math.floor(x1 / bucket_size))
        gy1 = int(math.floor(y1 / bucket_size))
        cells: List[Tuple[int, int]] = []
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                cells.append((gx, gy))
        return cells

    def _query_candidates(bb: Tuple[float, float, float, float]) -> List[int]:
        ids: List[int] = []
        seen: set[int] = set()
        for c in _bbox_to_cells(bb):
            for idx2 in spatial_cells.get(c, []):
                if idx2 in seen:
                    continue
                seen.add(idx2)
                ids.append(idx2)
        return ids

    def _insert_bbox(bb: Tuple[float, float, float, float], bbox_idx: int) -> None:
        for c in _bbox_to_cells(bb):
            spatial_cells.setdefault(c, []).append(bbox_idx)

    failures = 0
    fallback_nodes = 0
    total_attempts = 0
    total_collisions = 0
    early_exit_nodes = 0

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
        best_bbox = None
        best_xy = None
        best_collision = 10**9
        attempts = 0
        early_floor = min(int(max_tries), max(20, int(math.sqrt(max(1, len(nodes)))) * 8))

        for _ in range(int(max_tries)):
            attempts += 1
            total_attempts += 1
            cx = float(rng.uniform(x_lo, x_hi))
            cy = float(rng.uniform(y_lo, y_hi))
            n.setdefault("pos", {})
            n["pos"]["x"] = cx
            n["pos"]["y"] = cy

            bb = _expand_bbox(_node_bbox(n, vocab), pad=0.0)
            last_bbox = bb

            ok = True
            collision_count = 0
            for idx2 in _query_candidates(bb):
                bb2 = placed_bboxes[idx2]
                if _bbox_intersect(bb, bb2):
                    collision_count += 1
                    ok = False
            if collision_count < best_collision:
                best_collision = collision_count
                best_bbox = bb
                best_xy = (cx, cy)
            total_collisions += collision_count
            if ok:
                placed = True
                bbox_idx = len(placed_bboxes)
                placed_bboxes.append(bb)
                _insert_bbox(bb, bbox_idx)
                break

            fail_ratio = float(total_collisions) / float(max(1, total_attempts))
            if attempts >= early_floor and fail_ratio >= 0.90:
                early_exit_nodes += 1
                break

        if not placed:
            failures += 1
            fallback_nodes += 1
            # Use last sampled bbox if any; otherwise force center.
            if best_bbox is not None and best_xy is not None:
                n.setdefault("pos", {})
                n["pos"]["x"] = float(best_xy[0])
                n["pos"]["y"] = float(best_xy[1])
                last_bbox = best_bbox
            elif last_bbox is None:
                n.setdefault("pos", {})
                n["pos"]["x"] = float(w) / 2.0
                n["pos"]["y"] = float(h) / 2.0
                last_bbox = _node_bbox(n, vocab)
            bbox_idx = len(placed_bboxes)
            placed_bboxes.append(last_bbox)
            _insert_bbox(last_bbox, bbox_idx)

    if failures > 0:
        logger.warning("node_placement_failed", extra={"failures": failures, "num_nodes": len(nodes)})

    stats = {
        "num_nodes": int(len(nodes)),
        "total_attempts": int(total_attempts),
        "failed_nodes": int(failures),
        "fallback_nodes": int(fallback_nodes),
        "early_exit_nodes": int(early_exit_nodes),
        "collision_checks": int(total_collisions),
        "bucket_size": float(bucket_size),
        "duration_ms": float((time.perf_counter() - t_start) * 1000.0),
    }

    return nodes, stats


def _point_to_dict(p: Tuple[float, float]) -> Dict[str, float]:
    return {"x": float(p[0]), "y": float(p[1])}


def _node_map(scene: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for n in _iter_nodes(scene):
        nid = str(n.get("id") or "")
        if nid:
            out[nid] = n
    return out


def _endpoint_out_point(
    scene: Dict[str, Any], ep: Dict[str, Any], p: Tuple[float, float], vocab: Dict[str, Any], lead_len: float
) -> Tuple[float, float]:
    node_id = str(ep.get("node") or "")
    node = _node_map(scene).get(node_id)
    if not isinstance(node, dict):
        return (float(p[0]), float(p[1]))

    pos = node.get("pos") or {}
    cx = _safe_float(pos.get("x", 0.0), 0.0)
    cy = _safe_float(pos.get("y", 0.0), 0.0)

    vx = float(p[0]) - cx
    vy = float(p[1]) - cy
    norm = math.hypot(vx, vy)
    if norm <= 1e-6:
        return (float(p[0]) + float(lead_len), float(p[1]))

    k = float(lead_len) / norm
    return (float(p[0]) + vx * k, float(p[1]) + vy * k)


def _segment_intersects_bbox(a: Tuple[float, float], b: Tuple[float, float], bb: Tuple[float, float, float, float]) -> bool:
    x0, y0 = float(a[0]), float(a[1])
    x1, y1 = float(b[0]), float(b[1])
    bx0, by0, bx1, by1 = bb
    eps = 1e-6

    if abs(y0 - y1) <= eps:  # horizontal
        y = y0
        if y < by0 - eps or y > by1 + eps:
            return False
        sx0, sx1 = min(x0, x1), max(x0, x1)
        return not (sx1 < bx0 - eps or sx0 > bx1 + eps)

    if abs(x0 - x1) <= eps:  # vertical
        x = x0
        if x < bx0 - eps or x > bx1 + eps:
            return False
        sy0, sy1 = min(y0, y1), max(y0, y1)
        return not (sy1 < by0 - eps or sy0 > by1 + eps)

    return False


def _path_hits_any_bbox(path: Sequence[Tuple[float, float]], bboxes: Sequence[Tuple[float, float, float, float]]) -> bool:
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        for bb in bboxes:
            if _segment_intersects_bbox(a, b, bb):
                return True
    return False


def _compress_polyline(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not points:
        return []
    out: List[Tuple[float, float]] = [points[0]]
    for p in points[1:]:
        if abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    if len(out) <= 2:
        return out
    compact: List[Tuple[float, float]] = [out[0]]
    for i in range(1, len(out) - 1):
        a = compact[-1]
        b = out[i]
        c = out[i + 1]
        abx, aby = b[0] - a[0], b[1] - a[1]
        bcx, bcy = c[0] - b[0], c[1] - b[1]
        if abs(abx * bcy - aby * bcx) <= 1e-6 and (abs(abx) <= 1e-6 or abs(aby) <= 1e-6):
            continue
        compact.append(b)
    compact.append(out[-1])
    return compact


def _astar_manhattan_grid(
    start: Tuple[int, int],
    goal: Tuple[int, int],
    blocked: set[Tuple[int, int]],
    w_cells: int,
    h_cells: int,
) -> Optional[List[Tuple[int, int]]]:
    if start == goal:
        return [start]
    if start in blocked or goal in blocked:
        return None

    def h(p: Tuple[int, int]) -> int:
        return abs(p[0] - goal[0]) + abs(p[1] - goal[1])

    open_heap: List[Tuple[int, int, Tuple[int, int]]] = [(h(start), 0, start)]
    g_score: Dict[Tuple[int, int], int] = {start: 0}
    came: Dict[Tuple[int, int], Tuple[int, int]] = {}

    while open_heap:
        _, g_cur, cur = heapq.heappop(open_heap)
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            path.reverse()
            return path

        if g_cur != g_score.get(cur):
            continue

        cx, cy = cur
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if nx < 0 or ny < 0 or nx >= w_cells or ny >= h_cells:
                continue
            nxt = (nx, ny)
            if nxt in blocked:
                continue
            ng = g_cur + 1
            if ng < g_score.get(nxt, 1 << 30):
                came[nxt] = cur
                g_score[nxt] = ng
                heapq.heappush(open_heap, (ng + h(nxt), ng, nxt))
    return None


def route_net_orthogonal_avoid_obstacles(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    *,
    p0_out: Tuple[float, float],
    p1_out: Tuple[float, float],
    obstacles: Sequence[Tuple[float, float, float, float]],
    resolution: Tuple[int, int],
    grid_step: float = 12.0,
    margin_cells: int = 4,
) -> Optional[List[Dict[str, float]]]:
    step = max(4.0, float(grid_step))
    margin = max(1, int(margin_cells)) * step

    scene_w = max(step * 2.0, float(resolution[0]))
    scene_h = max(step * 2.0, float(resolution[1]))

    points = [p0, p1, p0_out, p1_out]
    min_x = min([0.0, *(float(p[0]) for p in points), *(float(bb[0]) for bb in obstacles)]) - margin
    min_y = min([0.0, *(float(p[1]) for p in points), *(float(bb[1]) for bb in obstacles)]) - margin
    max_x = max([scene_w, *(float(p[0]) for p in points), *(float(bb[2]) for bb in obstacles)]) + margin
    max_y = max([scene_h, *(float(p[1]) for p in points), *(float(bb[3]) for bb in obstacles)]) + margin

    span_w = max(step * 2.0, max_x - min_x)
    span_h = max(step * 2.0, max_y - min_y)
    w_cells = int(math.floor(span_w / step)) + 1
    h_cells = int(math.floor(span_h / step)) + 1

    def to_grid(p: Tuple[float, float]) -> Tuple[int, int]:
        gx = int(round((float(p[0]) - min_x) / step))
        gy = int(round((float(p[1]) - min_y) / step))
        return (min(max(gx, 0), w_cells - 1), min(max(gy, 0), h_cells - 1))

    def to_xy(g: Tuple[int, int]) -> Tuple[float, float]:
        return (float(min_x + g[0] * step), float(min_y + g[1] * step))

    blocked: set[Tuple[int, int]] = set()
    for bb in obstacles:
        x0, y0, x1, y1 = bb
        gx0 = max(0, int(math.floor((x0 - min_x) / step)))
        gy0 = max(0, int(math.floor((y0 - min_y) / step)))
        gx1 = min(w_cells - 1, int(math.ceil((x1 - min_x) / step)))
        gy1 = min(h_cells - 1, int(math.ceil((y1 - min_y) / step)))
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                blocked.add((gx, gy))

    s = to_grid(p0_out)
    t = to_grid(p1_out)
    blocked.discard(s)
    blocked.discard(t)
    grid_path = _astar_manhattan_grid(s, t, blocked, w_cells, h_cells)
    if not grid_path:
        return None

    centerline = [to_xy(g) for g in grid_path]
    full = [p0, p0_out, *centerline, p1_out, p1]
    compact = _compress_polyline(full)
    return [_point_to_dict(it) for it in compact]


def route_net_two_seg(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    *,
    p0_out: Optional[Tuple[float, float]] = None,
    p1_out: Optional[Tuple[float, float]] = None,
    bend_mode: str = "hv",
) -> List[Dict[str, float]]:
    """Return a polyline path: p0 -> p0_out -> trunk -> p1_out -> p1."""

    x0, y0 = float(p0[0]), float(p0[1])
    x1, y1 = float(p1[0]), float(p1[1])

    q0 = (x0, y0) if p0_out is None else (float(p0_out[0]), float(p0_out[1]))
    q1 = (x1, y1) if p1_out is None else (float(p1_out[0]), float(p1_out[1]))

    bm = (bend_mode or "hv").strip().lower()
    if bm not in ("hv", "vh"):
        bm = "hv"

    if bm == "hv":
        mid = (q1[0], q0[1])
    else:
        mid = (q0[0], q1[1])

    return [_point_to_dict((x0, y0)), _point_to_dict(q0), _point_to_dict(mid), _point_to_dict(q1), _point_to_dict((x1, y1))]


def route_all_nets(
    scene: Dict[str, Any],
    vocab: Dict[str, Any],
    mode: str = "two_seg",
    *,
    bend_mode: str = "hv",
    rng: Optional[np.random.Generator] = None,
    route_grid: float = 12.0,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
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
    lead_len = 12.0
    node_map = _node_map(scene)

    all_bboxes: Dict[str, Tuple[float, float, float, float]] = {}
    for node_id, node in node_map.items():
        all_bboxes[node_id] = _node_bbox(node, vocab)

    stats = {"success": 0, "degraded": 0, "failed": 0}
    avoid_mode = m in ("avoid_obstacles", "orthogonal", "maze")
    resolution = _get_resolution(scene)

    for net in _iter_nets(scene):
        fr = net.get("from") or net.get("from_")
        to = net.get("to")
        if not isinstance(fr, dict) or not isinstance(to, dict):
            net["path"] = []
            net["route_status"] = "failed"
            net["route_constraint_satisfied"] = False
            stats["failed"] += 1
            continue

        p0 = _endpoint_xy(scene, fr, vocab)
        p1 = _endpoint_xy(scene, to, vocab)

        from_node = str(fr.get("node") or "")
        to_node = str(to.get("node") or "")
        endpoint_boxes = [bb for nid, bb in all_bboxes.items() if nid in (from_node, to_node)]
        obstacles = [bb for nid, bb in all_bboxes.items() if nid not in (from_node, to_node)]

        if m == "straight":
            net["path"] = [_point_to_dict(p0), _point_to_dict(p1)]
            net["route_status"] = "degraded"
            net["route_constraint_satisfied"] = False
            stats["degraded"] += 1
            continue

        p0_out = _endpoint_out_point(scene, fr, p0, vocab, lead_len)
        p1_out = _endpoint_out_point(scene, to, p1, vocab, lead_len)

        chosen_path: List[Dict[str, float]]
        if avoid_mode:
            base_step = max(4.0, float(route_grid))
            routing_trials = (
                (base_step, 4),
                (max(4.0, base_step * 0.75), 6),
                (max(4.0, base_step * 0.5), 8),
                (max(4.0, base_step * 0.35), 10),
            )
            for trial_step, margin_cells in routing_trials:
                ortho = route_net_orthogonal_avoid_obstacles(
                    p0,
                    p1,
                    p0_out=p0_out,
                    p1_out=p1_out,
                    obstacles=obstacles,
                    resolution=resolution,
                    grid_step=trial_step,
                    margin_cells=margin_cells,
                )
                if ortho is None:
                    continue

                pts = [(float(it["x"]), float(it["y"])) for it in ortho]
                endpoint_ok = not _path_hits_any_bbox(pts[1:-1], endpoint_boxes)
                obstacle_ok = not _path_hits_any_bbox(pts, obstacles)
                if endpoint_ok and obstacle_ok:
                    net["path"] = ortho
                    net["route_status"] = "success"
                    net["route_constraint_satisfied"] = True
                    stats["success"] += 1
                    break
            else:
                ortho = None

            if ortho is not None and str(net.get("route_status") or "") == "success":
                continue

            fallback = route_net_two_seg(p0, p1, p0_out=p0_out, p1_out=p1_out, bend_mode="hv")
            fpts = [(float(it["x"]), float(it["y"])) for it in fallback]
            hard_ok = (not _path_hits_any_bbox(fpts[1:-1], endpoint_boxes)) and (not _path_hits_any_bbox(fpts, obstacles))
            net["path"] = fallback
            net["route_status"] = "degraded" if fallback else "failed"
            net["route_constraint_satisfied"] = bool(hard_ok)
            net["route_mode_used"] = "fallback_two_seg"
            if hard_ok:
                stats["success"] += 1
                net["route_status"] = "success"
            else:
                stats["degraded"] += 1
            continue

        if bm == "auto":
            hv_path = route_net_two_seg(p0, p1, p0_out=p0_out, p1_out=p1_out, bend_mode="hv")
            vh_path = route_net_two_seg(p0, p1, p0_out=p0_out, p1_out=p1_out, bend_mode="vh")
            hv_points = [(float(it["x"]), float(it["y"])) for it in hv_path]
            vh_points = [(float(it["x"]), float(it["y"])) for it in vh_path]

            hv_penalty = int(_path_hits_any_bbox(hv_points[1:-1], endpoint_boxes)) + int(_path_hits_any_bbox(hv_points, obstacles))
            vh_penalty = int(_path_hits_any_bbox(vh_points[1:-1], endpoint_boxes)) + int(_path_hits_any_bbox(vh_points, obstacles))

            if hv_penalty == vh_penalty and rng is not None:
                choice = "hv" if bool(rng.integers(0, 2)) else "vh"
            else:
                choice = "hv" if hv_penalty <= vh_penalty else "vh"
        else:
            choice = bm

        chosen_path = route_net_two_seg(p0, p1, p0_out=p0_out, p1_out=p1_out, bend_mode=choice)
        cpts = [(float(it["x"]), float(it["y"])) for it in chosen_path]
        hard_ok = (not _path_hits_any_bbox(cpts[1:-1], endpoint_boxes)) and (not _path_hits_any_bbox(cpts, obstacles))
        net["path"] = chosen_path
        net["route_constraint_satisfied"] = bool(hard_ok)
        if hard_ok:
            net["route_status"] = "success"
            stats["success"] += 1
        else:
            net["route_status"] = "degraded"
            stats["degraded"] += 1

    return scene, stats


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

    t_shuffle_start = time.perf_counter()

    # Defaults (v0.3-ish).
    margin = _safe_int(p.get("margin", 20), 20)
    requested_max_tries = _safe_int(p.get("max_tries", 2000), 2000)
    node_count = int(len(list(_iter_nodes(scene))))
    max_tries_cap = max(120, min(5000, 120 + 25 * max(1, node_count)))
    max_tries = max(10, min(requested_max_tries, max_tries_cap))
    placement = str(p.get("placement", p.get("placement_mode", "random_nonoverlap")) or "random_nonoverlap")
    route_mode = str(p.get("route_mode", p.get("route", "avoid_obstacles")) or "avoid_obstacles")
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

    t_place_start = time.perf_counter()
    if placement.strip().lower() in ("random_nonoverlap", "nonoverlap", "random"):
        _, placement_stats = place_nodes_random_nonoverlap(
            nodes, vocab=vocab, resolution=res, margin=margin, max_tries=max_tries, rng=rng
        )
    else:
        # Unknown placement strategy: fallback to non-overlap.
        logger.warning("unknown_placement_strategy", extra={"placement": placement})
        _, placement_stats = place_nodes_random_nonoverlap(
            nodes, vocab=vocab, resolution=res, margin=margin, max_tries=max_tries, rng=rng
        )
    t_place_end = time.perf_counter()

    # Write back nodes list to preserve all fields (copy.deepcopy created new list).
    # Our `nodes` list contains the same dict objects as in scene_out["nodes"],
    # so positions are already updated.

    t_route_start = time.perf_counter()
    if return_paths:
        _, route_stats = route_all_nets(
            scene_out,
            vocab=vocab,
            mode=route_mode,
            bend_mode=bend_mode,
            rng=rng,
            route_grid=_safe_float(p.get("route_grid", 12.0), 12.0),
        )
    else:
        # Explicitly clear paths to keep output compact.
        for net in _iter_nets(scene_out):
            net["path"] = []
            net["route_status"] = "failed"
            net["route_constraint_satisfied"] = False
        route_stats = {"success": 0, "degraded": 0, "failed": int(len(list(_iter_nets(scene_out))))}

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
                    "max_tries_requested": requested_max_tries,
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
        "route_stats": route_stats,
    }

    return scene_out, meta
