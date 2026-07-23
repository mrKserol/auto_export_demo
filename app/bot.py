from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.database import Database
from app.handlers.customer_add import router as customer_add_router
from app.handlers.customer_delete import router as customer_delete_router
from app.handlers.customers import router as customers_router
from app.handlers.documents import router as documents_router
from app.handlers.estimates import router as estimates_router
from app.handlers.recognize import router as recognize_router
from app.handlers.specifications import router as specifications_router
from app.handlers.start import router as start_router
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.yandex_gpt_service import YandexGPTService
from app.services.yandex_ocr_service import YandexOCRService
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(start_router)
    dispatcher.include_router(customer_add_router)
    dispatcher.include_router(customers_router)
    dispatcher.include_router(customer_delete_router)
    dispatcher.include_router(specifications_router)
    dispatcher.include_router(estimates_router)
    dispatcher.include_router(recognize_router)
    dispatcher.include_router(documents_router)
    return dispatcher


async def run_bot(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = create_bot(settings)
    dispatcher = create_dispatcher()
    database = Database(settings.database_url)
    yandex_disk_client = YandexDiskClient(
        token=settings.yandex_disk_token,
        base_path=settings.yandex_disk_base_path,
    )
    yandex_function_client = (
        YandexFunctionClient(settings.yandex_function_url)
        if settings.yandex_function_url
        else None
    )
    yandex_ocr_service = YandexOCRService(
        api_key=settings.yandex_api_key,
        min_delay_seconds=settings.ocr_min_delay_seconds,
        max_retries=settings.max_ocr_retries,
    )
    yandex_gpt_service = YandexGPTService(
        api_key=settings.yandex_api_key,
        folder_id=settings.yandex_cloud_folder_id,
    )
    customer_document_recognition_service = CustomerDocumentRecognitionService(
        ocr_service=yandex_ocr_service,
        gpt_service=yandex_gpt_service,
    )

    await database.connect()
    await yandex_disk_client.ensure_base_path()

    try:
        await dispatcher.start_polling(
            bot,
            database=database,
            yandex_disk_client=yandex_disk_client,
            yandex_function_client=yandex_function_client,
            customer_document_recognition_service=customer_document_recognition_service,
            enable_processing=settings.enable_processing,
        )
    finally:
        await database.close()
        await bot.session.close()
