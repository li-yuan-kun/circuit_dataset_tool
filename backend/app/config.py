"""backend/app/config.py

Circuit Dataset Tool (v0.3) configuration.

Implements the Settings + get_settings() contract defined in the v0.3 design
document.

Key properties:
- Defaults are project-root relative (so `uvicorn backend.app.main:app` works out
  of the box when run from the repo).
- Environment variables can override any field (via pydantic-settings).
- Paths are normalized (expanduser/resolve) and essential output directories are
  created automatically.

Environment variables
---------------------
All fields can be overridden with the prefix `CDT_`.
Examples:
  CDT_DATASET_ROOT=./backend/dataset_output
  CDT_ENABLE_JOBS=true
  CDT_DEFAULT_OCC_THRESHOLD=0.85

You may also use a `.env` file at the repo root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root_from_here() -> Path:
    """Infer repo root from this file location.

    Expected layout (v0.3):
      <root>/backend/app/config.py
    """

    return Path(__file__).resolve().parents[2]


def _default_dataset_root() -> Path:
    return _repo_root_from_here() / "backend" / "dataset_output"


def _default_vocab_path() -> Path:
    return _repo_root_from_here() / "shared" / "vocab.json"


def _default_footprint_dir() -> Path:
    return _repo_root_from_here() / "shared" / "footprints"


def _default_cors_allow_origins() -> list[str]:
    """Default CORS origins for local frontend development."""

    return [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]


class Settings(BaseSettings):
    """Application settings.

    Notes:
      - `MANIFEST_PATH` can be left unset; it will default to
        `DATASET_ROOT/manifest.jsonl`.
      - Paths are stored as absolute paths after validation.
    """

    model_config = SettingsConfigDict(
        env_prefix="CDT_",
        env_file=(str(_repo_root_from_here() / ".env"), str(_repo_root_from_here() / "backend" / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Versioning / API
    TOOL_VERSION: str = "0.3"
    API_PREFIX: str = "/api/v1"

    # Data paths
    DATASET_ROOT: Path = _default_dataset_root()
    VOCAB_PATH: Path = _default_vocab_path()
    FOOTPRINT_DIR: Path = _default_footprint_dir()
    MANIFEST_PATH: Optional[Path] = None

    # Defaults for algorithms
    DEFAULT_OCC_THRESHOLD: float = 0.9
    DEFAULT_RESOLUTION_W: int = 1024
    DEFAULT_RESOLUTION_H: int = 1024

    # Optional features
    ENABLE_JOBS: bool = False

    # CORS
    CORS_ALLOW_ORIGINS: list[str] = _default_cors_allow_origins()
    CORS_ALLOW_CREDENTIALS: bool = False

    # -----------------
    # Validators
    # -----------------

    @field_validator("API_PREFIX")
    @classmethod
    def _normalize_api_prefix(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return "/api/v1"
        if not v.startswith("/"):
            v = "/" + v
        # Avoid trailing slash except for root.
        if len(v) > 1 and v.endswith("/"):
            v = v[:-1]
        return v

    @field_validator("DEFAULT_OCC_THRESHOLD")
    @classmethod
    def _validate_occ_threshold(cls, v: float) -> float:
        if not (0.0 <= float(v) <= 1.0):
            raise ValueError("DEFAULT_OCC_THRESHOLD must be within [0, 1]")
        return float(v)

    @field_validator("DEFAULT_RESOLUTION_W", "DEFAULT_RESOLUTION_H")
    @classmethod
    def _validate_resolution(cls, v: int) -> int:
        v_int = int(v)
        if v_int <= 0:
            raise ValueError("Resolution must be a positive integer")
        return v_int

    @field_validator("CORS_ALLOW_ORIGINS", mode="before")
    @classmethod
    def _coerce_cors_allow_origins(cls, v):
        if v is None:
            return _default_cors_allow_origins()
        if isinstance(v, str):
            # Support comma-separated env format:
            # CDT_CORS_ALLOW_ORIGINS=http://127.0.0.1:5173,https://example.com
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(item).strip() for item in v if str(item).strip()]
        raise ValueError("CORS_ALLOW_ORIGINS must be a list or comma-separated string")

    @field_validator("DATASET_ROOT", "VOCAB_PATH", "FOOTPRINT_DIR", "MANIFEST_PATH", mode="before")
    @classmethod
    def _coerce_path(cls, v):
        if v is None or isinstance(v, Path):
            return v
        return Path(str(v))

    @field_validator("DATASET_ROOT", "VOCAB_PATH", "FOOTPRINT_DIR", "MANIFEST_PATH")
    @classmethod
    def _normalize_path(cls, v: Optional[Path]) -> Optional[Path]:
        if v is None:
            return None
        return v.expanduser().resolve()

    @model_validator(mode="after")
    def _finalize(self) -> "Settings":
        if not self.CORS_ALLOW_ORIGINS:
            self.CORS_ALLOW_ORIGINS = _default_cors_allow_origins()

        # Derive MANIFEST_PATH from DATASET_ROOT if not provided.
        if self.MANIFEST_PATH is None:
            self.MANIFEST_PATH = (self.DATASET_ROOT / "manifest.jsonl").resolve()

        # Ensure output dirs exist.
        try:
            self.DATASET_ROOT.mkdir(parents=True, exist_ok=True)
            if self.MANIFEST_PATH is not None:
                self.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Don't hard-fail at import time; IO errors should surface when saving.
            pass

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings with environment overrides (singleton cache)."""

    return Settings()
