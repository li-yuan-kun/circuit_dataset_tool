"""backend/app/services/exporter.py

Sample exporter utilities.

This module is called by `api/routers/dataset.py` to persist one complete sample
(image/mask/scene/label) under the dataset root.

Expected directory layout (v0.3+):
  DATASET_ROOT/
    sample_000001/
      image.png
      mask.png
      scene.json
      label.json

The functions here are pure I/O + metadata helpers; they do not depend on
FastAPI.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .storage import Storage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _stable_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_json(obj: Any) -> str:
    return _sha256_bytes(_stable_json_bytes(obj))




def _compose_image_with_mask(image_bytes: bytes, mask_bytes: bytes) -> Optional[bytes]:
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        mask = Image.open(io.BytesIO(mask_bytes)).convert("L")
        if image.size != mask.size:
            mask = mask.resize(image.size)

        arr = np.asarray(image, dtype=np.uint8).copy()
        mask_arr = np.asarray(mask, dtype=np.uint8) > 0
        arr[mask_arr, 0] = 255
        arr[mask_arr, 1] = (arr[mask_arr, 1] * 0.35).astype(np.uint8)
        arr[mask_arr, 2] = (arr[mask_arr, 2] * 0.35).astype(np.uint8)

        out = io.BytesIO()
        Image.fromarray(arr, mode="RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def allocate_sample_id(storage: Storage, *, prefix: str = "sample_", width: int = 6) -> str:
    """Allocate a new sample id.

    Strategy:
    - If `storage` exposes `.root` and it is a directory, scan existing
      directories matching `prefix + digits` and pick max+1.
    - Otherwise, fall back to a timestamp-based id.
    """
    root: Optional[Path] = None
    try:
        r = getattr(storage, "root", None)
        if r is not None:
            root = Path(r)
    except Exception:
        root = None

    if root is not None and root.exists() and root.is_dir():
        mx = 0
        for p in root.iterdir():
            if not p.is_dir():
                continue
            name = p.name
            if not name.startswith(prefix):
                continue
            suf = name[len(prefix) :]
            if not suf.isdigit():
                continue
            try:
                mx = max(mx, int(suf))
            except Exception:
                continue
        nxt = mx + 1
        return f"{prefix}{nxt:0{int(width)}d}"

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}{ts}"


def save_sample(
    storage: Storage,
    *,
    image_bytes: bytes,
    mask_bytes: bytes,
    scene_obj: Dict[str, Any],
    label_obj: Dict[str, Any],
    sample_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a full sample to storage.

    Returns:
        {
          "ok": True,
          "sample_id": ...,
          "paths": {"image":..., "mask":..., "scene":..., "label":...},
          "hashes": {...},
          "timestamp": ...
        }
    """
    if not image_bytes:
        raise ValueError("image_bytes is empty")
    if not mask_bytes:
        raise ValueError("mask_bytes is empty")
    if not isinstance(scene_obj, dict):
        raise ValueError("scene_obj must be a dict")
    if not isinstance(label_obj, dict):
        raise ValueError("label_obj must be a dict")

    sid = (sample_id or "").strip() or allocate_sample_id(storage)
    if "/" in sid or "\\" in sid or sid.startswith("."):
        raise ValueError(f"invalid sample_id: {sid}")

    sample_dir = sid

    # Safer default: do not overwrite existing sample directory.
    try:
        abs_dir = Path(storage.get_abs_path(sample_dir))
        if abs_dir.exists() and any(abs_dir.iterdir()):
            raise FileExistsError(f"sample_id already exists: {sid}")
    except FileNotFoundError:
        pass
    except Exception:
        pass

    storage.ensure_dir(sample_dir)

    rel_image = f"{sample_dir}/image.png"
    rel_mask = f"{sample_dir}/mask.png"
    rel_scene = f"{sample_dir}/scene.json"
    rel_label = f"{sample_dir}/label.json"
    rel_image_with_mask = f"{sample_dir}/image_with_mask.png"

    p_image = storage.put_bytes(rel_image, image_bytes)
    p_mask = storage.put_bytes(rel_mask, mask_bytes)
    p_scene = storage.put_json(rel_scene, scene_obj)
    p_label = storage.put_json(rel_label, label_obj)

    overlay_bytes = _compose_image_with_mask(image_bytes, mask_bytes)
    p_image_with_mask: Optional[str] = None
    if overlay_bytes:
        p_image_with_mask = storage.put_bytes(rel_image_with_mask, overlay_bytes)

    hashes = {
        "image": _sha256_bytes(image_bytes),
        "mask": _sha256_bytes(mask_bytes),
        "scene": _sha256_json(scene_obj),
        "label": _sha256_json(label_obj),
    }
    if overlay_bytes:
        hashes["image_with_mask"] = _sha256_bytes(overlay_bytes)

    paths = {"image": p_image, "mask": p_mask, "scene": p_scene, "label": p_label}
    if p_image_with_mask:
        paths["image_with_mask"] = p_image_with_mask

    return {
        "ok": True,
        "sample_id": sid,
        "paths": paths,
        "hashes": hashes,
        "timestamp": _now_iso(),
    }
