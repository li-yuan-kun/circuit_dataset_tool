"""backend/app/main.py

Circuit Dataset Tool (v0.3) FastAPI entry.

This module follows the v0.3 design document:
- create_app(): build app (routers / middlewares / exception handlers / health check)
- include_routers(): mount /api/v1 routers
- register_middlewares(): CORS, request_id, timing, etc.
- register_exception_handlers(): unified error response
- healthz(): GET /healthz (also exposed at {API_PREFIX}/healthz)
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def _get_logger() -> logging.Logger:
    # Prefer project logger if available.
    try:
        from .logging import get_logger  # type: ignore

        return get_logger(__name__)
    except Exception:
        return logging.getLogger(__name__)


def _error_payload(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register unified error format.

    Design doc requires:
      {"error": {"code": "...", "message": "...", "details": {...}}}
    """

    logger = _get_logger()

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
        # If routers raise HTTPException with detail as dict, preserve it.
        details: Dict[str, Any] = {}
        code = "HTTP_ERROR"
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # already in our format
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        if isinstance(exc.detail, dict):
            # allow routers to pass code/message/details style
            code = str(exc.detail.get("code", code))
            message = str(exc.detail.get("message", message))
            details = exc.detail.get("details", details) or {}

        logger.info(
            "http_exception",
            extra={
                "path": request.url.path,
                "status_code": exc.status_code,
                "code": code,
            },
        )
        return JSONResponse(status_code=exc.status_code, content=_error_payload(code, message, details))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(request: Request, exc: RequestValidationError):
        logger.info(
            "validation_error",
            extra={
                "path": request.url.path,
                "errors": exc.errors(),
            },
        )
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                code="REQUEST_VALIDATION_ERROR",
                message="Request body validation failed",
                details={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc_handler(request: Request, exc: Exception):
        logger.exception(
            "unhandled_exception",
            extra={
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                code="INTERNAL_ERROR",
                message="Internal server error",
                details={},
            ),
        )


def register_middlewares(app: FastAPI) -> None:
    """Register CORS / request_id / timing middleware."""

    logger = _get_logger()

    cors_allow_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
    cors_allow_credentials = False

    try:
        from .config import get_settings  # type: ignore

        settings = get_settings()
        cors_allow_origins = list(getattr(settings, "CORS_ALLOW_ORIGINS", cors_allow_origins) or cors_allow_origins)
        cors_allow_credentials = bool(getattr(settings, "CORS_ALLOW_CREDENTIALS", False))
    except Exception:
        logger.warning("settings_load_failed_for_cors")

    if cors_allow_credentials and "*" in cors_allow_origins:
        explicit_origins = [origin for origin in cors_allow_origins if origin != "*"]
        if explicit_origins:
            cors_allow_origins = explicit_origins
        else:
            cors_allow_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
        logger.warning(
            "cors_wildcard_not_allowed_with_credentials",
            extra={"origins": cors_allow_origins},
        )

    # CORS: local-dev friendly defaults, configurable with CDT_CORS_ALLOW_*.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Prefer project middlewares if provided.
    try:
        from .logging import register_request_id_middleware, request_timing_middleware  # type: ignore

        register_request_id_middleware(app)
        app.middleware("http")(request_timing_middleware)
        return
    except Exception:
        pass

    @app.middleware("http")
    async def _request_id_and_timing(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "latency_ms": round(latency_ms, 3),
                },
            )

        response.headers["x-request-id"] = rid
        response.headers["x-latency-ms"] = f"{latency_ms:.3f}"
        return response


def include_routers(app: FastAPI) -> None:
    """Mount all routers under /api/v1."""

    settings = getattr(app.state, "settings", None)
    api_prefix = getattr(settings, "API_PREFIX", "/api/v1")

    # Routers are defined in backend/app/api/routers/*.py in the design.
    from .api.routers.scene import router as scene_router  # type: ignore
    from .api.routers.mask import router as mask_router  # type: ignore
    from .api.routers.label import router as label_router  # type: ignore
    from .api.routers.topology import router as topology_router  # type: ignore
    from .api.routers.dataset import router as dataset_router  # type: ignore

    app.include_router(scene_router, prefix=api_prefix)
    app.include_router(mask_router, prefix=api_prefix)
    app.include_router(label_router, prefix=api_prefix)
    app.include_router(topology_router, prefix=api_prefix)
    app.include_router(dataset_router, prefix=api_prefix)

    # Optional jobs router.
    if getattr(settings, "ENABLE_JOBS", False):
        from .api.routers.jobs import router as jobs_router  # type: ignore

        app.include_router(jobs_router, prefix=api_prefix)


def healthz(app: FastAPI) -> Dict[str, Any]:
    """Health payload for GET /healthz and {API_PREFIX}/healthz."""

    settings = getattr(app.state, "settings", None)
    return {
        "ok": True,
        "tool_version": getattr(settings, "TOOL_VERSION", "0.3"),
        "api_prefix": getattr(settings, "API_PREFIX", "/api/v1"),
        "enable_jobs": bool(getattr(settings, "ENABLE_JOBS", False)),
    }


def create_app() -> FastAPI:
    """Build FastAPI app (routers / middlewares / exception handlers / health check)."""

    # Settings & logging
    try:
        from .config import get_settings  # type: ignore

        settings = get_settings()
    except Exception:
        settings = None

    try:
        from .logging import setup_logging  # type: ignore

        setup_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Load shared resources once (vocab / footprints) and attach to app.state.
        logger = _get_logger()
        if settings is not None:
            app.state.settings = settings

        try:
            from .core_logic.rasterize import load_vocab, load_footprints  # type: ignore

            if settings is not None:
                vocab = load_vocab(settings.VOCAB_PATH)
                footprint_db = load_footprints(settings.FOOTPRINT_DIR, vocab)
                app.state.vocab = vocab
                app.state.footprint_db = footprint_db
                logger.info("resources_loaded", extra={"vocab_version": vocab.get("vocab_version")})
        except Exception as e:
            # Do not block service startup in early development; routers may still work if they don't need these.
            logger.warning("resource_load_failed", extra={"error": str(e)})

        yield

    tool_version = getattr(settings, "TOOL_VERSION", "0.3")
    api_prefix = getattr(settings, "API_PREFIX", "/api/v1")

    app = FastAPI(
        title="Circuit Dataset Tool API",
        version=str(tool_version),
        lifespan=lifespan,
        docs_url=f"{api_prefix}/docs",
        redoc_url=f"{api_prefix}/redoc",
        openapi_url=f"{api_prefix}/openapi.json",
    )

    register_middlewares(app)
    register_exception_handlers(app)
    include_routers(app)

    @app.get("/healthz", tags=["health"])
    @app.get(f"{api_prefix}/healthz", tags=["health"])
    async def _healthz():
        return healthz(app)

    @app.get(api_prefix, tags=["meta"])
    async def _api_root():
        """A friendly API root to avoid 404 on GET /api/v1."""

        return {
            "ok": True,
            "message": "Circuit Dataset Tool API",
            "docs": f"{api_prefix}/docs",
            "openapi": f"{api_prefix}/openapi.json",
        }

    return app


# ASGI entry
app = create_app()
