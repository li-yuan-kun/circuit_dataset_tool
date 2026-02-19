"""backend/app/services/storage.py

Storage abstraction for dataset outputs.

This service layer is intentionally small and stable. Routers use it to persist
files under the configured DATASET_ROOT.

Design (v0.3+):
- `Storage` protocol: ensure_dir / put_bytes / put_json / get_abs_path
- `LocalStorage`: filesystem implementation rooted at a directory.

Robustness notes
----------------
- Prevents path traversal by forcing all writes to stay under `root`.
- Uses atomic writes (write to temp then replace) for bytes + json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Protocol, Union


class Storage(Protocol):
    """Abstract storage backend."""

    def ensure_dir(self, rel_dir: str) -> str:
        """Ensure a directory exists; returns normalized relative path."""

    def put_bytes(self, rel_path: str, data: bytes) -> str:
        """Write bytes to rel_path; returns normalized relative path."""

    def put_json(self, rel_path: str, obj: Dict[str, Any]) -> str:
        """Write JSON to rel_path; returns normalized relative path."""

    def get_abs_path(self, rel_path: str) -> str:
        """Resolve a relative path into an absolute path (backend-specific)."""


def _norm_rel(rel: Union[str, Path]) -> str:
    s = str(rel).replace("\\", "/").strip()
    while s.startswith("/"):
        s = s[1:]
    s = os.path.normpath(s).replace("\\", "/")
    if s in ("", "."):
        return ""
    if s.startswith("../") or s == ".." or "/../" in f"/{s}/":
        raise ValueError(f"Unsafe relative path: {rel}")
    return s


class LocalStorage:
    """Local filesystem storage rooted at `root`."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, rel_path: Union[str, Path]) -> Path:
        rel = _norm_rel(rel_path)
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except Exception as e:
            raise ValueError(f"Path escapes storage root: {rel_path}") from e
        return p

    def get_abs_path(self, rel_path: str) -> str:
        return str(self._abs(rel_path))

    def ensure_dir(self, rel_dir: str) -> str:
        rel = _norm_rel(rel_dir)
        p = self._abs(rel)
        p.mkdir(parents=True, exist_ok=True)
        return rel

    def put_bytes(self, rel_path: str, data: bytes) -> str:
        if data is None:
            raise ValueError("data is None")
        rel = _norm_rel(rel_path)
        p = self._abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd = None
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
            with os.fdopen(tmp_fd, "wb") as f:
                tmp_fd = None
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, p)
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except Exception:
                    pass
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        return rel

    def put_json(self, rel_path: str, obj: Dict[str, Any]) -> str:
        data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        return self.put_bytes(rel_path, data)

    # Optional helpers (not required by routers)
    def exists(self, rel_path: str) -> bool:  # pragma: no cover
        try:
            return self._abs(rel_path).exists()
        except Exception:
            return False

    def list_dir(self, rel_dir: str = "") -> list[str]:  # pragma: no cover
        p = self._abs(rel_dir)
        if not p.exists() or not p.is_dir():
            return []
        return sorted([x.name for x in p.iterdir()])