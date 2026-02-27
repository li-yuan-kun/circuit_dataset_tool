"""backend/app/jobs/tasks.py  (optional)

In-process job/task implementation.

This module backs the optional Jobs API.

Public API
----------
- submit_job(job_type: str, payload: dict) -> str
- get_job_status(job_id: str) -> dict

The worker loop lives in `backend/app/jobs/worker.py` and calls `execute_job()`.

Implementation goals
--------------------
- Keep dependencies light (stdlib + existing core_logic + services).
- Make the jobs feature usable in a single-process deployment by starting a
  daemon worker thread on-demand (when submit_job() is called).
- Store job state in memory (good enough for v0.3/v0.4 development). You can
  later replace this with Redis/Celery/RQ without changing the router contract.
"""

from __future__ import annotations

import copy
import json
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

try:
    from ..logging import get_logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore
        return logging.getLogger(name)


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = Lock()

# Vocab cache (loaded from settings.VOCAB_PATH).
_VOCAB_CACHE: Optional[Dict[str, Any]] = None
_VOCAB_CACHE_PATH: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_job_id() -> str:
    return uuid.uuid4().hex


def _safe_copy(obj: Any) -> Any:
    try:
        return copy.deepcopy(obj)
    except Exception:
        return obj


def _job_get(job_id: str) -> Optional[Dict[str, Any]]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _safe_copy(job) if job else None


def _job_set(job_id: str, **fields: Any) -> None:
    """Update a job record in-place (thread-safe)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_at"] = _now_iso()


def _job_init(job_id: str, job_type: str, payload: Dict[str, Any]) -> None:
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",  # queued|running|succeeded|failed
            "progress": 0.0,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "result": None,
            "error": None,
            # Keep payload for debugging; can be removed or truncated later.
            "payload": _safe_copy(payload),
        }


# ---------------------------------------------------------------------------
# Settings / storage / resources
# ---------------------------------------------------------------------------

def _get_settings():
    try:
        from ..config import get_settings  # type: ignore

        return get_settings()
    except Exception:
        return None


def _get_storage(settings):
    try:
        from ..services.storage import LocalStorage  # type: ignore

        return LocalStorage(getattr(settings, "DATASET_ROOT"))
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Storage service not available: {e}") from e


def _get_vocab(settings) -> Dict[str, Any]:
    global _VOCAB_CACHE, _VOCAB_CACHE_PATH

    vocab_path = str(getattr(settings, "VOCAB_PATH"))
    if _VOCAB_CACHE is not None and _VOCAB_CACHE_PATH == vocab_path:
        return _VOCAB_CACHE

    from ..core_logic.rasterize import load_vocab  # type: ignore

    vocab = load_vocab(Path(vocab_path))
    _VOCAB_CACHE = vocab
    _VOCAB_CACHE_PATH = vocab_path
    return vocab


def _job_rel_dir(job_id: str) -> str:
    # Under DATASET_ROOT
    return f"_jobs/{job_id}"


def _zip_directory(abs_dir: str, abs_zip_path: str) -> None:
    """Create a zip file containing all files under abs_dir."""
    base = Path(abs_dir)
    zip_p = Path(abs_zip_path)
    zip_p.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in base.rglob("*"):
            if p.is_file():
                arcname = str(p.relative_to(base)).replace("\\", "/")
                zf.write(p, arcname=arcname)


def _extract_scenes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Best-effort extraction of scenes list from payload."""
    scenes = payload.get("scenes")
    if isinstance(scenes, list):
        return [s for s in scenes if isinstance(s, dict)]

    scene = payload.get("scene")
    if isinstance(scene, dict):
        return [scene]

    # allow scene_paths: list[str]
    paths = payload.get("scene_paths") or payload.get("scenes_paths")
    if isinstance(paths, list):
        out: List[Dict[str, Any]] = []
        for p in paths:
            if not isinstance(p, str):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out

    # allow items: [{scene: {...}}, ...]
    items = payload.get("items")
    if isinstance(items, list):
        out2: List[Dict[str, Any]] = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("scene"), dict):
                out2.append(it["scene"])
        return out2

    return []


def _scene_seed(scene: Dict[str, Any], fallback: int = 0) -> int:
    try:
        return int((scene.get("meta") or {}).get("seed") or fallback)
    except Exception:
        return int(fallback)


def _scene_resolution(scene: Dict[str, Any], settings) -> Tuple[int, int]:
    meta = scene.get("meta") or {}
    res = meta.get("resolution") or {}
    try:
        w = int(res.get("w") or getattr(settings, "DEFAULT_RESOLUTION_W", 1024))
        h = int(res.get("h") or getattr(settings, "DEFAULT_RESOLUTION_H", 1024))
        return w, h
    except Exception:
        return 1024, 1024


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_manifest_record(*, sample_id: str, saved_paths: Dict[str, Any], scene_obj: Dict[str, Any], label_obj: Dict[str, Any], settings) -> Dict[str, Any]:
    meta = (scene_obj or {}).get("meta", {}) if isinstance(scene_obj, dict) else {}
    return {
        "sample_id": sample_id,
        "paths": saved_paths,
        "function": (label_obj or {}).get("function"),
        "counts_visible": (label_obj or {}).get("counts_visible"),
        "seed": meta.get("seed"),
        "tool_version": getattr(settings, "TOOL_VERSION", "0.3") if settings is not None else "0.3",
        "vocab_version": meta.get("vocab_version"),
        "timestamp": _now_iso(),
    }


def _rasterize_scene_png(scene: Dict[str, Any], footprint_db: Any, settings) -> bytes:
    from PIL import Image, ImageDraw  # type: ignore

    from ..core_logic.rasterize import render_footprint_on_canvas  # type: ignore

    w, h = _scene_resolution(scene, settings)
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    for node in (scene.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        comp_type = str(node.get("type") or "")
        if not comp_type:
            continue
        try:
            fp = render_footprint_on_canvas(node=node, footprint_db=footprint_db, resolution=(w, h))
        except Exception:
            continue
        canvas[np.asarray(fp, dtype=np.uint8) > 0] = (255, 0, 0)

    # Draw net polylines in red so saved batch images keep visible wiring as colored output.
    img = Image.fromarray(canvas, mode="RGB")
    draw = ImageDraw.Draw(img)
    for net in (scene.get("nets") or []):
        if not isinstance(net, dict):
            continue
        path = net.get("path") or []
        if not isinstance(path, list) or len(path) < 2:
            continue
        pts = []
        for p in path:
            if not isinstance(p, dict):
                continue
            try:
                x = float(p.get("x"))
                y = float(p.get("y"))
            except Exception:
                continue
            pts.append((x, y))
        if len(pts) >= 2:
            draw.line(pts, fill=(255, 0, 0), width=3)

    import io

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _decode_mask_png(mask_png: bytes) -> np.ndarray:
    import io

    from PIL import Image  # type: ignore

    img = Image.open(io.BytesIO(mask_png)).convert("L")
    return np.asarray(img, dtype=np.uint8)


def _compose_image_with_mask(image_png: bytes, mask_png: bytes) -> bytes:
    import io

    from PIL import Image  # type: ignore

    image = Image.open(io.BytesIO(image_png)).convert("RGB")
    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    if image.size != mask.size:
        mask = mask.resize(image.size)

    arr = np.asarray(image, dtype=np.uint8).copy()
    mask_arr = np.asarray(mask, dtype=np.uint8) > 0
    arr[mask_arr] = (255, 255, 255)
    out = io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Public API expected by router
# ---------------------------------------------------------------------------

def submit_job(job_type: str, payload: dict) -> str:
    """Submit a job and return a job_id."""
    jt = str(job_type or "generic").strip() or "generic"
    job_id = _new_job_id()
    _job_init(job_id, jt, dict(payload or {}))

    # Start worker and enqueue.
    from .worker import ensure_worker_started, enqueue_job  # local import to avoid cycles

    ensure_worker_started()
    enqueue_job(job_id, jt, dict(payload or {}))
    return job_id


def get_job_status(job_id: str) -> dict:
    """Return job status dict, or {} if missing."""
    return _job_get(job_id) or {}


# ---------------------------------------------------------------------------
# Task runners
# ---------------------------------------------------------------------------

def run_batch_shuffle(payload: dict, *, job_id: str | None = None) -> dict:
    """Batch shuffle scenes via core_logic.topology.shuffle_scene()."""
    settings = _get_settings()
    if settings is None:
        raise RuntimeError("Settings not available")

    scenes = _extract_scenes(dict(payload or {}))
    params = dict((payload or {}).get("params") or (payload or {}).get("shuffle_params") or {})
    return_paths = bool((payload or {}).get("return_paths", True))
    save_outputs = bool((payload or {}).get("save", True))
    make_zip = bool((payload or {}).get("zip", False))

    vocab = _get_vocab(settings)
    storage = _get_storage(settings) if save_outputs else None

    out_items: List[Dict[str, Any]] = []
    rel_dir = _job_rel_dir(job_id or _new_job_id())

    if storage is not None:
        storage.ensure_dir(rel_dir)

    from ..core_logic.topology import shuffle_scene  # type: ignore

    total = max(len(scenes), 1)
    for i, scene in enumerate(scenes):
        seed = _scene_seed(scene, fallback=int((payload or {}).get("seed") or 0))
        scene_shuf, meta = shuffle_scene(scene=scene, vocab=vocab, params=params, seed=seed, return_paths=return_paths)

        item: Dict[str, Any] = {"index": i, "meta": meta}

        if storage is not None:
            rel_path = f"{rel_dir}/scene_{i:06d}.json"
            saved = storage.put_json(rel_path, scene_shuf)
            item["scene_path"] = saved

        out_items.append(item)

        if job_id is not None:
            _job_set(job_id, progress=float(i + 1) / float(total))

    result: Dict[str, Any] = {
        "ok": True,
        "job_type": "batch_shuffle",
        "num_items": len(out_items),
        "items": out_items,
    }

    if storage is not None:
        abs_dir = storage.get_abs_path(rel_dir)
        paths: Dict[str, Any] = {"dir": rel_dir, "abs_dir": abs_dir}

        if make_zip:
            abs_zip = storage.get_abs_path(f"{rel_dir}.zip")
            _zip_directory(abs_dir, abs_zip)
            paths["zip"] = f"{rel_dir}.zip"
            paths["abs_zip"] = abs_zip

        result["paths"] = paths

    return result


def run_batch_mask(payload: dict, *, job_id: str | None = None) -> dict:
    """Batch generate masks via core_logic.mask_gen.generate_mask()."""
    settings = _get_settings()
    if settings is None:
        raise RuntimeError("Settings not available")

    scenes = _extract_scenes(dict(payload or {}))
    strategy = str((payload or {}).get("strategy") or (payload or {}).get("mask_strategy") or "value_noise").strip()
    params = dict((payload or {}).get("params") or (payload or {}).get("mask_params") or {})
    save_outputs = bool((payload or {}).get("save", True))
    make_zip = bool((payload or {}).get("zip", False))

    storage = _get_storage(settings) if save_outputs else None
    rel_dir = _job_rel_dir(job_id or _new_job_id())
    if storage is not None:
        storage.ensure_dir(rel_dir)

    from ..core_logic.mask_gen import generate_mask, encode_png  # type: ignore

    out_items: List[Dict[str, Any]] = []
    total = max(len(scenes), 1)

    if not scenes:
        # Allow generation without a scene (use payload resolution or defaults).
        base_seed = int((payload or {}).get("seed") or 0)
        resolution = (payload or {}).get("resolution") or {
            "w": getattr(settings, "DEFAULT_RESOLUTION_W", 1024),
            "h": getattr(settings, "DEFAULT_RESOLUTION_H", 1024),
        }
        mask_np, meta = generate_mask(strategy=strategy, resolution=resolution, scene=None, params=params, seed=base_seed)
        png = encode_png(mask_np)
        item: Dict[str, Any] = {"index": 0, "meta": meta}
        if storage is not None:
            rel_path = f"{rel_dir}/mask_{0:06d}.png"
            saved = storage.put_bytes(rel_path, png)
            item["mask_path"] = saved
        out_items.append(item)
    else:
        for i, scene in enumerate(scenes):
            seed = _scene_seed(scene, fallback=int((payload or {}).get("seed") or 0))
            w, h = _scene_resolution(scene, settings)
            mask_np, meta = generate_mask(strategy=strategy, resolution=(w, h), scene=scene, params=params, seed=seed)
            png = encode_png(mask_np)

            item2: Dict[str, Any] = {"index": i, "meta": meta}
            if storage is not None:
                rel_path = f"{rel_dir}/mask_{i:06d}.png"
                saved = storage.put_bytes(rel_path, png)
                item2["mask_path"] = saved
            out_items.append(item2)

            if job_id is not None:
                _job_set(job_id, progress=float(i + 1) / float(total))

    result: Dict[str, Any] = {
        "ok": True,
        "job_type": "batch_mask",
        "num_items": len(out_items),
        "items": out_items,
    }

    if storage is not None:
        abs_dir = storage.get_abs_path(rel_dir)
        paths: Dict[str, Any] = {"dir": rel_dir, "abs_dir": abs_dir}

        if make_zip:
            abs_zip = storage.get_abs_path(f"{rel_dir}.zip")
            _zip_directory(abs_dir, abs_zip)
            paths["zip"] = f"{rel_dir}.zip"
            paths["abs_zip"] = abs_zip

        result["paths"] = paths

    return result


def run_batch_dataset(payload: dict, *, job_id: str | None = None) -> dict:
    """Unified batch dataset generation: shuffle -> image -> mask -> label -> save."""
    settings = _get_settings()
    if settings is None:
        raise RuntimeError("Settings not available")

    from ..core_logic.mask_gen import encode_png, generate_mask  # type: ignore
    from ..core_logic.occlusion import compute_label  # type: ignore
    from ..core_logic.topology import shuffle_scene  # type: ignore
    from ..services.exporter import save_sample  # type: ignore
    from ..services.manifest import append_record  # type: ignore

    storage = _get_storage(settings)
    footprint_db = _get_vocab(settings)

    base_scene = (payload or {}).get("scene")
    scenes = _extract_scenes(dict(payload or {}))
    if isinstance(base_scene, dict) and not scenes:
        scenes = [base_scene]
    if not scenes:
        raise ValueError("batch_dataset requires payload.scene or payload.scenes")

    n = max(1, int((payload or {}).get("n") or len(scenes) or 1))
    seed_start = int((payload or {}).get("seed_start") or (payload or {}).get("seed") or 0)
    use_shuffle = bool((payload or {}).get("use_backend_shuffle", True))
    shuffle_params = dict((payload or {}).get("shuffle_params") or {})
    mask_strategy = str((payload or {}).get("mask_strategy") or "perlin")
    mask_params = dict((payload or {}).get("mask_params") or {})
    occ_threshold = float((payload or {}).get("occ_threshold") or 0.9)
    function_name = str((payload or {}).get("function") or "UNKNOWN")
    save_prefix = str((payload or {}).get("sample_prefix") or f"batch_{_now_compact()}_")
    make_zip = bool((payload or {}).get("zip", False))

    rel_dir = _job_rel_dir(job_id or _new_job_id())
    storage.ensure_dir(rel_dir)
    out_items: List[Dict[str, Any]] = []
    attempt_errors: List[Dict[str, Any]] = []
    succeeded = 0
    failed = 0
    attempt = 0
    max_attempts = max(n, int((payload or {}).get("max_attempts") or (n * 20)))

    while succeeded < n and attempt < max_attempts:
        seed = seed_start + attempt
        source_scene = copy.deepcopy(scenes[attempt % len(scenes)])
        try:
            scene_item = source_scene
            if use_shuffle:
                scene_item, _ = shuffle_scene(
                    scene=source_scene,
                    vocab=footprint_db,
                    params=shuffle_params,
                    seed=seed,
                    return_paths=True,
                )

            image_png = _rasterize_scene_png(scene_item, footprint_db, settings)
            w, h = _scene_resolution(scene_item, settings)
            mask_np, mask_meta = generate_mask(
                strategy=mask_strategy,
                resolution=(w, h),
                scene=scene_item,
                params=mask_params,
                seed=seed,
            )
            mask_png = encode_png(mask_np)
            mask_np_decoded = _decode_mask_png(mask_png)
            label = compute_label(
                scene=scene_item,
                mask=mask_np_decoded,
                footprint_db=footprint_db,
                function=function_name,
                occ_threshold=occ_threshold,
            )
            image_with_mask = _compose_image_with_mask(image_png, mask_png)

            sample_id = f"{save_prefix}{succeeded:06d}"
            saved = save_sample(
                storage,
                image_bytes=image_png,
                mask_bytes=mask_png,
                scene_obj=scene_item,
                label_obj=label,
                sample_id=sample_id,
            )
            saved_paths = dict(saved.get("paths") or {})
            rel_overlay = f"{sample_id}/image_with_mask.png"
            saved_paths["image_with_mask"] = storage.put_bytes(rel_overlay, image_with_mask)

            append_record(
                settings.MANIFEST_PATH,
                _build_manifest_record(
                    sample_id=sample_id,
                    saved_paths=saved_paths,
                    scene_obj=scene_item,
                    label_obj=label,
                    settings=settings,
                ),
            )

            out_items.append(
                {
                    "index": succeeded,
                    "attempt": attempt,
                    "seed": seed,
                    "sample_id": sample_id,
                    "paths": {
                        "image": saved_paths.get("image"),
                        "mask": saved_paths.get("mask"),
                        "image_with_mask": saved_paths.get("image_with_mask"),
                        "label": saved_paths.get("label"),
                        "scene": saved_paths.get("scene"),
                    },
                    "mask_meta": mask_meta,
                }
            )
            succeeded += 1
        except Exception as e:
            failed += 1
            attempt_errors.append({"attempt": attempt, "seed": seed, "error": str(e)})
        finally:
            attempt += 1

        if job_id is not None:
            _job_set(job_id, progress=float(succeeded) / float(max(n, 1)))

    result: Dict[str, Any] = {
        "ok": failed == 0,
        "job_type": "batch_dataset",
        "num_items": len(out_items),
        "target_n": n,
        "succeeded": succeeded,
        "failed": failed,
        "attempted": attempt,
        "max_attempts": max_attempts,
        "items": out_items,
        "errors": attempt_errors,
    }

    abs_dir = storage.get_abs_path(rel_dir)
    paths: Dict[str, Any] = {"dir": rel_dir, "abs_dir": abs_dir}
    if make_zip:
        abs_zip = storage.get_abs_path(f"{rel_dir}.zip")
        zip_path = Path(abs_zip)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in out_items:
                sample_id = item.get("sample_id")
                pths = item.get("paths") or {}
                if not sample_id or not isinstance(pths, dict):
                    continue
                for key in ("image", "mask", "image_with_mask", "label", "scene"):
                    rel_file = pths.get(key)
                    if not isinstance(rel_file, str):
                        continue
                    try:
                        abs_file = Path(storage.get_abs_path(rel_file))
                    except Exception:
                        continue
                    if not abs_file.exists() or not abs_file.is_file():
                        continue
                    zf.write(abs_file, arcname=f"{sample_id}/{key}{abs_file.suffix}")
        paths["zip"] = f"{rel_dir}.zip"
        paths["abs_zip"] = abs_zip
    result["paths"] = paths
    return result


# ---------------------------------------------------------------------------
# Worker dispatch entry
# ---------------------------------------------------------------------------

def execute_job(job_id: str, job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a job and update status in the job store.

    Returns:
        result dict (also written to job status).
    """
    _job_set(job_id, status="running", progress=0.0, error=None, result=None)

    jt = (job_type or "generic").strip().lower()

    try:
        if jt in ("batch_shuffle", "shuffle", "topology_shuffle", "shuffle_scene"):
            result = run_batch_shuffle(payload, job_id=job_id)
        elif jt in ("batch_mask", "mask", "generate_mask", "mask_generate"):
            result = run_batch_mask(payload, job_id=job_id)
        elif jt in ("batch_dataset", "dataset_batch", "batch_generate_dataset"):
            result = run_batch_dataset(payload, job_id=job_id)
        else:
            # Generic no-op job for early integration testing.
            result = {"ok": True, "job_type": jt, "echo": payload}

        _job_set(job_id, status="succeeded", progress=1.0, result=result, error=None)
        return result

    except Exception as e:
        err = {
            "message": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc(limit=20),
        }
        logger.exception("job_failed", extra={"job_id": job_id, "job_type": jt})
        _job_set(job_id, status="failed", error=err, result=None)
        return {"ok": False, "error": err}


# ---------------------------------------------------------------------------
# Maintenance helpers (optional)
# ---------------------------------------------------------------------------

def cleanup_expired(ttl_seconds: int) -> int:
    """Remove completed jobs older than ttl_seconds. Returns number removed."""
    if ttl_seconds <= 0:
        return 0

    now = datetime.now(timezone.utc).timestamp()
    removed = 0

    with _JOBS_LOCK:
        to_del: List[str] = []
        for jid, st in _JOBS.items():
            if not isinstance(st, dict):
                continue
            if st.get("status") not in ("succeeded", "failed"):
                continue
            ts_s = st.get("updated_at") or st.get("created_at")
            try:
                ts = datetime.fromisoformat(str(ts_s).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = now
            if (now - ts) >= ttl_seconds:
                to_del.append(jid)

        for jid in to_del:
            _JOBS.pop(jid, None)
            removed += 1

    return removed
