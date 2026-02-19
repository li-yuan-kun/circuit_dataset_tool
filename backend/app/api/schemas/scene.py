"""backend/app/api/schemas/scene.py

Scene data protocol models (v0.3).

These models describe the canonical shape of a circuit "scene":
- meta: versioning + randomness seed + resolution
- nodes: component instances
- nets: connectivity between nodes/pins
- mask: optional reference to an occlusion mask

Routers in this project are currently permissive and may accept partially
specified scenes (e.g., missing some `meta` fields). Therefore, many meta
fields are optional here as well.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict


class Point(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x: float
    y: float


class Resolution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    w: int
    h: int


class SceneMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene_version: str = Field(default="0.3")
    tool_version: str = Field(default="0.3")
    vocab_version: Optional[str] = None
    seed: Optional[int] = None
    resolution: Optional[Resolution] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[str] = Field(default=None, description="ISO-8601 string")


class Node(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    pos: Point
    rot: float = 0.0
    scale: float = 1.0


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node: str
    pin: str


class Net(BaseModel):
    """Connection between two endpoints.

    Note: "from" is reserved in Python, so we expose it as `from_` with alias.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    from_: Endpoint = Field(alias="from")
    to: Endpoint
    path: List[Point] = Field(default_factory=list)


class MaskRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["external", "generated"]
    path: Optional[str] = None
    hash: Optional[str] = None
    strategy: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class Scene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    meta: SceneMeta = Field(default_factory=SceneMeta)
    nodes: List[Node] = Field(default_factory=list)
    nets: List[Net] = Field(default_factory=list)
    mask: Optional[MaskRef] = None
