from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.database import Database
from app.web.miniapp_routes import router as miniapp_router
from app.web.routes import router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_fastapi_app(
    *,
    settings: Settings,
    database: Database,
    bot,
    yandex_disk_client=None,
    customer_batch_recognition_service=None,
    customer_folder_service=None,
) -> FastAPI:
    app = FastAPI(title="Auto Export Mini App", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.bot = bot
    app.state.yandex_disk_client = yandex_disk_client
    app.state.customer_batch_recognition_service = customer_batch_recognition_service
    app.state.customer_folder_service = customer_folder_service

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.include_router(miniapp_router)
    return app
