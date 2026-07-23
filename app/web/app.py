from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.database import Database
from app.web.routes import router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_fastapi_app(
    *,
    settings: Settings,
    database: Database,
    bot,
) -> FastAPI:
    app = FastAPI(title="Auto Export Mini App", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.bot = bot

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return app
