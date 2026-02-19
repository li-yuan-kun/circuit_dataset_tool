"""backend/app/api/schemas/requests.py

HTTP request schemas (v0.3).

Important compatibility note
----------------------------
Routers in `backend/app/api/routers/*` currently treat `req.scene` as a plain
`dict` and access it via `.get(...)`. Therefore, request models in this module
store `scene` as `Dict[str, Any]` (even though a stronger `Scene` model exists
in `schemas/scene.py`).

To keep the API ergonomic, these models accept either:
- a raw dict, or
- a `Scene`/pydantic BaseModel instance
and normalize it to a dict.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.config import ConfigDict

try:
    # Optional: for callers that want typed construction.
    from .scene import Scene
except Exception:  # pragma: no cover
    Scene = None  # type: ignore


def _as_dict(v: Any) -> Dict[str, Any]:
    """Normalize scene-like inputs to a JSON-serializable dict."""

    if v is None:
        return {}

    if isinstance(v, dict):
        return v

    # pydantic BaseModel (including our Scene model)
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump(by_alias=True)
        except Exception:
            # fall back
            try:
                return dict(v)  # type: ignore[arg-type]
            except Exception:
                return {}

    # best-effort
    try:
        return dict(v)  # type: ignore[arg-type]
    except Exception:
        return {}


SceneLike = Union[Dict[str, Any], "Scene"] if Scene is not None else Dict[str, Any]


class ValidateSceneRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene: Dict[str, Any]
    strict: bool = False

    @field_validator("scene", mode="before")
    @classmethod
    def _scene_to_dict(cls, v: Any) -> Dict[str, Any]:
        return _as_dict(v)


class GenerateMaskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene: Dict[str, Any]
    strategy: str
    params: Dict[str, Any] = Field(default_factory=dict)

    # Router contract (current): if true, return raw PNG bytes.
    return_bytes: bool = False

    # Backward/forward compatibility: also accept the design-doc field.
    # This is not used by routers directly; we map it to return_bytes.
    return_mode: Optional[Literal["png_base64", "bytes"]] = Field(default=None, exclude=True)

    @field_validator("scene", mode="before")
    @classmethod
    def _scene_to_dict(cls, v: Any) -> Dict[str, Any]:
        return _as_dict(v)

    @model_validator(mode="after")
    def _coerce_return_mode(self) -> "GenerateMaskRequest":
        if self.return_mode is not None:
            self.return_bytes = bool(self.return_mode == "bytes")
        return self


class ComputeLabelRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene: Dict[str, Any]
    mask_png_base64: str
    occ_threshold: Optional[float] = None
    function: str = ""

    @field_validator("scene", mode="before")
    @classmethod
    def _scene_to_dict(cls, v: Any) -> Dict[str, Any]:
        return _as_dict(v)


class ShuffleSceneRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene: Dict[str, Any]
    params: Dict[str, Any] = Field(default_factory=dict)
    return_paths: bool = True

    @field_validator("scene", mode="before")
    @classmethod
    def _scene_to_dict(cls, v: Any) -> Dict[str, Any]:
        return _as_dict(v)


class DatasetSaveJsonRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sample_id: Optional[str] = None
    image_png_base64: str
    mask_png_base64: str
    scene: Dict[str, Any]
    label: Dict[str, Any]

    @field_validator("scene", "label", mode="before")
    @classmethod
    def _obj_to_dict(cls, v: Any) -> Dict[str, Any]:
        return _as_dict(v)
