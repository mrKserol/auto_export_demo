from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.database import Database
from app.services.document_recognition_service import process_uploaded_document
from app.states.customer_states import RecognizeDocumentStates
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


router = Router(name="recognize")


@router.message(Command("recognize_document"))
async def handle_recognize_document(message: Message, state: FSMContext) -> None:
    await state.set_state(RecognizeDocumentStates.waiting_document)
    await message.answer("Загрузите документ для распознавания.")


@router.message(
    StateFilter(RecognizeDocumentStates.waiting_document),
    F.document,
)
async def handle_recognize_document_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    database: Database,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
) -> None:
    result = await process_uploaded_document(
        message=message,
        bot=bot,
        database=database,
        yandex_disk_client=yandex_disk_client,
        yandex_function_client=yandex_function_client,
        enable_processing=enable_processing,
    )
    await state.clear()
    await message.reply(result["reply_text"])
