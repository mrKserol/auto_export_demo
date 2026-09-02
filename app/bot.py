from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.database import Database
from app.handlers.customer_add import router as customer_add_router
from app.handlers.customer_batch_upload import router as customer_batch_upload_router
from app.handlers.customer_delete import router as customer_delete_router
from app.handlers.customers import router as customers_router
from app.handlers.documents import router as documents_router
from app.handlers.estimates import router as estimates_router
from app.handlers.recognize import router as recognize_router
from app.handlers.specifications import router as specifications_router
from app.handlers.start import router as start_router
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.services.customer_batch_recognition_service import (
    CustomerBatchRecognitionService,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.customer_folder_service import CustomerFolderService
from app.services.estimate_recognition_service import EstimateRecognitionService
from app.services.miniapp_background_tasks import pending_miniapp_task_count
from app.services.miniapp_batch_api_service import (
    recover_stale_miniapp_batches_on_startup,
)
from app.services.yandex_gpt_service import YandexGPTService
from app.services.yandex_ocr_service import YandexOCRService
from app.web.app import create_fastapi_app
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


def create_bot(settings: Settings) -> Bot:
    bot_kwargs: dict = {
        "token": settings.telegram_bot_token,
        "default": DefaultBotProperties(parse_mode=ParseMode.HTML),
    }
    if settings.telegram_proxy_url:
        bot_kwargs["session"] = AiohttpSession(proxy=settings.telegram_proxy_url)
    return Bot(**bot_kwargs)


async def _stop_polling_if_started(dispatcher: Dispatcher) -> None:
    try:
        await dispatcher.stop_polling()
    except RuntimeError as exc:
        if "Polling is not started" not in str(exc):
            raise


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(start_router)
    dispatcher.include_router(customer_batch_upload_router)
    dispatcher.include_router(customer_add_router)
    dispatcher.include_router(customers_router)
    dispatcher.include_router(customer_delete_router)
    dispatcher.include_router(specifications_router)
    dispatcher.include_router(estimates_router)
    dispatcher.include_router(recognize_router)
    dispatcher.include_router(documents_router)
    return dispatcher


async def run_bot(settings: Settings) -> None:
    await run_application(settings)


async def run_application(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    # Log application commit SHA for diagnostics
    logger.info("Application commit SHA: %s", settings.app_commit_sha or "unknown")
    if not settings.miniapp_allowed_telegram_user_ids:
        if settings.miniapp_test_mode:
            logger.warning(
                "MINIAPP_ALLOWED_TELEGRAM_USER_IDS is empty; "
                "Mini App APIs deny all users "
                "(MINIAPP_TEST_MODE does not bypass the allowlist)"
            )
        else:
            logger.error(
                "MINIAPP_ALLOWED_TELEGRAM_USER_IDS is empty; "
                "Mini App employee APIs are locked"
            )
    else:
        logger.info(
            "Mini App employee allowlist configured (%s user ids)",
            len(settings.miniapp_allowed_telegram_user_ids),
        )
    if settings.telegram_proxy_url:
        logger.info("Telegram proxy enabled")
    else:
        logger.info("Telegram proxy disabled")

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
    estimate_recognition_service = EstimateRecognitionService(
        ocr_service=yandex_ocr_service,
        gpt_service=yandex_gpt_service,
    )

    await database.connect()
    await yandex_disk_client.ensure_base_path()
    customer_upload_batch_repository = CustomerUploadBatchRepository(database.pool)
    customer_batch_recognition_service = CustomerBatchRecognitionService(
        repository=customer_upload_batch_repository,
        recognition_service=customer_document_recognition_service,
    )
    customer_folder_service = CustomerFolderService(
        repository=customer_upload_batch_repository,
        yandex_disk_client=yandex_disk_client,
    )

    recovered = await recover_stale_miniapp_batches_on_startup(
        customer_upload_batch_repository,
        stale_after_seconds=settings.customer_batch_stale_processing_seconds,
    )
    if recovered:
        logger.info(
            "Startup recovered %s Mini App batch(es) stuck in recognizing",
            len(recovered),
        )

    fastapi_app = create_fastapi_app(
        settings=settings,
        database=database,
        bot=bot,
        yandex_disk_client=yandex_disk_client,
        customer_batch_recognition_service=customer_batch_recognition_service,
        customer_folder_service=customer_folder_service,
    )
    uvicorn_config = uvicorn.Config(
        fastapi_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        loop="asyncio",
    )
    web_server = uvicorn.Server(uvicorn_config)

    polling_task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            settings=settings,
            database=database,
            yandex_disk_client=yandex_disk_client,
            yandex_function_client=yandex_function_client,
            customer_document_recognition_service=customer_document_recognition_service,
            estimate_recognition_service=estimate_recognition_service,
            customer_upload_batch_repository=customer_upload_batch_repository,
            customer_batch_recognition_service=customer_batch_recognition_service,
            customer_folder_service=customer_folder_service,
            enable_processing=settings.enable_processing,
        ),
        name="aiogram-polling",
    )
    web_task = asyncio.create_task(web_server.serve(), name="uvicorn")

    try:
        done, pending = await asyncio.wait(
            {polling_task, web_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if exception is not None:
                logger.error(
                    "Background task failed: %s",
                    exception,
                    exc_info=exception,
                )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        unfinished = pending_miniapp_task_count()
        if unfinished:
            logger.warning(
                "Shutting down with %s unfinished Mini App background task(s); "
                "stale recognizing batches will be recovered on next startup",
                unfinished,
            )
        web_server.should_exit = True
        await _stop_polling_if_started(dispatcher)
        await database.close()
        await bot.session.close()
