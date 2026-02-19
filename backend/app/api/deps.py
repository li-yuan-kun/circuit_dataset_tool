from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException, Request


def _dependency_error(code: str, message: str, *, status_code: int = 500, details: Dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def get_settings(request: Request):
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings

    try:
        from ...config import get_settings as _load_settings  # type: ignore

        settings = _load_settings()
        request.app.state.settings = settings
        return settings
    except Exception as exc:
        raise _dependency_error("SETTINGS_UNAVAILABLE", "failed to load application settings", details={"error": str(exc)})


def get_vocab(request: Request):
    vocab = getattr(request.app.state, "vocab", None)
    if isinstance(vocab, dict):
        return vocab

    settings = get_settings(request)

    try:
        from ...core_logic.rasterize import load_vocab  # type: ignore

        vocab = load_vocab(settings.VOCAB_PATH)
        request.app.state.vocab = vocab
        return vocab
    except Exception as exc:
        raise _dependency_error("VOCAB_UNAVAILABLE", "failed to load vocab", details={"error": str(exc)})


def get_footprint_db(request: Request):
    footprint_db = getattr(request.app.state, "footprint_db", None)
    if footprint_db is not None:
        return footprint_db

    settings = get_settings(request)
    vocab = get_vocab(request)

    try:
        from ...core_logic.rasterize import load_footprints  # type: ignore

        footprint_db = load_footprints(settings.FOOTPRINT_DIR, vocab)
        request.app.state.footprint_db = footprint_db
        return footprint_db
    except Exception as exc:
        raise _dependency_error("FOOTPRINT_DB_UNAVAILABLE", "failed to load footprint database", details={"error": str(exc)})
