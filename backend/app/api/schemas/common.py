"""backend/app/api/schemas/common.py

Common HTTP-layer schemas (v0.3).

The API uses a unified error payload:
  {"error": {"code": "...", "message": "...", "details": {...}}}

Routers and exception handlers may return this structure directly.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Inner error structure."""

    code: str = Field(..., description="Stable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Dict[str, Any] = Field(default_factory=dict, description="Optional extra details")


class ErrorResponse(BaseModel):
    """Unified error response wrapper."""

    error: ErrorDetail
