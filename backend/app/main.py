"""InvoiceGuard AI - FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import settings
from .database import get_engine, init_db, session_factory
from .usage_limiter import UsageLimiter

logging.basicConfig(level=logging.INFO)

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
# When the React app is built (npm run build -> frontend/dist), serve the
# bundle; otherwise fall back to the raw source directory.
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"


def _frontend_mount_dir() -> Path | None:
    if (FRONTEND_DIST_DIR / "index.html").exists():
        return FRONTEND_DIST_DIR
    if (FRONTEND_DIR / "index.html").exists():
        return FRONTEND_DIR
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    engine = get_engine(settings.db_path)
    app.state.engine = engine
    app.state.session_factory = session_factory(settings.db_path)
    app.state.limiter = UsageLimiter(engine)
    yield
    engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="InvoiceGuard AI",
        description=(
            "Compares supplier invoices against Purchase Orders with semantic matching "
            "and deterministic financial validation."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            content={
                "status": "ok",
                "service": "InvoiceGuard AI",
                "time": datetime.now(timezone.utc).isoformat(),
            }
        )

    samples_dir = BACKEND_DIR.parent / "samples"
    if samples_dir.exists():
        app.mount("/samples", StaticFiles(directory=str(samples_dir)), name="samples")

    frontend_dir = _frontend_mount_dir()
    if frontend_dir is not None:
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    else:  # keep the API usable when only the backend directory is present
        @app.get("/")
        def index() -> JSONResponse:
            return JSONResponse(
                content={
                    "service": "InvoiceGuard AI",
                    "message": "Static frontend not found; API endpoints are available.",
                    "docs": "/docs",
                }
            )

    return app


app = create_app()
