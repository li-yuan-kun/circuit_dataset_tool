"""backend/app/services/manifest.py

Manifest (JSONL) utilities.

The dataset manifest is an append-only JSON Lines file:
  <DATASET_ROOT>/manifest.jsonl

Each line is a JSON object describing one saved sample.
Routers typically construct the record payload, and this module persists it.

Primary API (v0.3+):
- append_record(manifest_path, record)
- load_records(manifest_path, limit=None)
- compute_dataset_stats(manifest_path)

Concurrency
-----------
No cross-platform file lock is implemented here. In common single-process
deployments this is sufficient. For multi-worker concurrent writers, consider
adding a lock or building the manifest offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _as_path(p: Any) -> Path:
    if isinstance(p, Path):
        return p
    return Path(str(p)).expanduser().resolve()


def append_record(manifest_path: Any, record: Dict[str, Any]) -> None:
    """Append one JSON record to manifest.jsonl."""
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")

    mp = _as_path(manifest_path)
    mp.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with mp.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
        f.flush()


def _iter_lines(mp: Path) -> Iterable[str]:
    if not mp.exists() or not mp.is_file():
        return []

    def _gen():
        with mp.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    yield s

    return _gen()


def load_records(manifest_path: Any, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load records from manifest.jsonl (tolerates malformed lines)."""
    mp = _as_path(manifest_path)
    if not mp.exists() or not mp.is_file():
        return []

    records: List[Dict[str, Any]] = []
    for s in _iter_lines(mp):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                records.append(obj)
        except Exception:
            continue

    if limit is not None and limit > 0 and len(records) > limit:
        return records[-int(limit) :]
    return records


def compute_stats(manifest_path: Any) -> Dict[str, Any]:
    """Compute lightweight dataset stats from the manifest."""
    records = load_records(manifest_path)
    num_samples = len(records)

    fn_counts: Dict[str, int] = {}
    comp_counts: Dict[str, int] = {}

    for r in records:
        if not isinstance(r, dict):
            continue
        fn = r.get("function")
        if isinstance(fn, str) and fn:
            fn_counts[fn] = fn_counts.get(fn, 0) + 1

        counts_visible = r.get("counts_visible")
        if isinstance(counts_visible, dict):
            for k, v in counts_visible.items():
                try:
                    comp_counts[str(k)] = comp_counts.get(str(k), 0) + int(v)
                except Exception:
                    continue

    return {
        "num_samples": num_samples,
        "function_counts": fn_counts,
        "component_counts_visible_sum": comp_counts,
    }


# Compatibility alias (design-doc name)
def compute_dataset_stats(manifest_path: Any) -> Dict[str, Any]:
    return compute_stats(manifest_path)