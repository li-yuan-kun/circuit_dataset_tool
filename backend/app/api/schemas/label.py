"""backend/app/api/schemas/label.py

Label (occlusion) protocol models (v0.3).

A label is produced by occluding a scene with a mask and computing which
components are still sufficiently visible.

Core fields expected by routers / manifest builder:
- counts_all: counts per component type in the full scene
- counts_visible: counts per type after occlusion
- occlusion: per-node occlusion ratios (0..1)
- occ_threshold: threshold used to decide visibility
- function: task label (e.g., "ADC" / "DAC" / "UNKNOWN")
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict


class OcclusionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    type: str
    occ_ratio: float = Field(..., ge=0.0, le=1.0, description="Occlusion ratio in [0, 1]")


class Label(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label_version: str = Field(default="0.3")
    counts_all: Dict[str, int] = Field(default_factory=dict)
    counts_visible: Dict[str, int] = Field(default_factory=dict)
    occlusion: List[OcclusionItem] = Field(default_factory=list)
    occ_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    function: str = Field(default="UNKNOWN")
    meta: Dict[str, Any] = Field(default_factory=dict)
