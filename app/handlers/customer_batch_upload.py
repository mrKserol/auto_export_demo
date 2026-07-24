from __future__ import annotations

import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_recognition_service import (
    CustomerBatchRecognitionService,
)
from app.services.customer_file_download_service import (
    CustomerFileDownloadError,
    download_customer_file_from_telegram,
)
from app.states.customer_states import CustomerBatchUploadStates


router = Router(name="customer_batch_upload")
logger = logging.getLogger(__name__)

REQUIRED_DOCUMENTS_COUNT = 4

ADD_CUSTOMER_INTRO = (
    "Добавление клиента\n\n"
    "Отправьте четыре документа одним альбомом или несколькими сообщениями:\n\n"
    "1. Главную страницу паспорта\n"
    "2. Страницу паспорта с регистрацией\n"
    "3. СНИЛС\n"
    "4. ИНН\n\n"
    "Порядок отправки не имеет значения.\n\n"
    "Поддерживаются фотографии и PDF-файлы.\n"
    "После загрузки нажмите «Обработать документы»."
)


@router.message(Command("add_customer"))
async def handle_add_customer_batch(
    message: Message,
    state: FSMContext,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if message.from_user is None or message.chat is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    batch = await customer_upload_batch_repository.get_active_batch(chat_id, user_id)

    if batch is not None and batch["status"] == CustomerUploadBatchStatus.COLLECTING:
        logger.info(
            "customer batch restored batch_id=%s telegram_chat_id=%s "
            "telegram_user_id=%s media_group_id=%s",
            batch["id"],
            chat_id,
            user_id,
            batch.get("media_group_id"),
        )
    else:
        batch = await customer_upload_batch_repository.create_batch(
            batch_key=f"tg-{chat_id}-{user_id}-{uuid.uuid4().hex}",
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
        )
        logger.info(
            "customer batch created batch_id=%s telegram_chat_id=%s "
            "telegram_user_id=%s media_group_id=%s",
            batch["id"],
            chat_id,
            user_id,
            batch.get("media_group_id"),
        )

    file_count = await customer_upload_batch_repository.count_batch_files(batch["id"])
    await state.set_state(CustomerBatchUploadStates.collecting_documents)
    await state.update_data(batch_id=batch["id"])
    await message.answer(
        ADD_CUSTOMER_INTRO,
        reply_markup=_batch_keyboard(file_count),
    )


@router.message(
    StateFilter(CustomerBatchUploadStates.collecting_documents),
    F.document | F.photo,
)
async def handle_batch_document_in_state(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    await _accept_batch_document(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        repository=customer_upload_batch_repository,
        allow_restore=False,
    )


@router.message(StateFilter(None), F.document | F.photo)
async def handle_batch_document_restore(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    await _accept_batch_document(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        repository=customer_upload_batch_repository,
        allow_restore=True,
    )


@router.callback_query(F.data == "customer_batch:process")
@router.callback_query(F.data == "customer_batch:retry_recognition")
async def handle_batch_process(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
    customer_batch_recognition_service: CustomerBatchRecognitionService,
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    batch = await _resolve_batch_for_callback(
        callback=callback,
        state=state,
        repository=customer_upload_batch_repository,
    )
    if batch is None:
        await callback.message.answer(
            "Активный пакет документов не найден.\n"
            "Начните заново командой /add_customer"
        )
        await callback.answer()
        return

    status = batch["status"]
    if status == CustomerUploadBatchStatus.ABANDONED:
        await callback.message.answer("Загрузка клиента отменена.")
        await callback.answer()
        return

    if status in {
        CustomerUploadBatchStatus.RECOGNIZED,
        CustomerUploadBatchStatus.CREATING_FOLDER,
        CustomerUploadBatchStatus.UPLOADING,
        CustomerUploadBatchStatus.FILES_SAVED,
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
    }:
        await callback.message.answer("Этот пакет уже передан в обработку.")
        await callback.answer()
        return

    file_count = await customer_upload_batch_repository.count_batch_files(batch["id"])
    if (
        status == CustomerUploadBatchStatus.COLLECTING
        and file_count < REQUIRED_DOCUMENTS_COUNT
    ):
        await callback.message.answer(
            _format_insufficient_files_message(file_count)
        )
        await callback.answer()
        return

    await state.set_state(CustomerBatchUploadStates.processing_documents)
    await state.update_data(batch_id=batch["id"])
    await callback.message.answer(
        "Распознаю документы.\n"
        f"Получено файлов: {file_count}\n\n"
        "Это может занять некоторое время."
    )
    await callback.answer()

    logger.info(
        "customer batch submitted for processing batch_id=%s telegram_chat_id=%s "
        "telegram_user_id=%s media_group_id=%s file_count=%s status=%s",
        batch["id"],
        batch.get("telegram_chat_id"),
        batch.get("telegram_user_id"),
        batch.get("media_group_id"),
        file_count,
        status,
    )

    chat_id = callback.message.chat.id
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        result = await customer_batch_recognition_service.process_batch(batch["id"])
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        logger.exception(
            "customer batch recognition handler failed batch_id=%s",
            batch["id"],
        )
        await callback.message.answer(
            "Не удалось завершить распознавание документов.\n\n"
            "Полученные файлы сохранены. Попробуйте запустить обработку повторно.",
            reply_markup=_recognition_retry_keyboard(),
        )
        return

    reply_markup = None
    if result.is_technical_failure or not result.kit.is_complete:
        reply_markup = _recognition_retry_keyboard()

    await callback.message.answer(
        result.telegram_message,
        reply_markup=reply_markup,
    )


@router.callback_query(F.data == "customer_batch:cancel")
async def handle_batch_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    batch = await _resolve_batch_for_callback(
        callback=callback,
        state=state,
        repository=customer_upload_batch_repository,
    )
    if batch is not None and batch["status"] == CustomerUploadBatchStatus.COLLECTING:
        await customer_upload_batch_repository.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.ABANDONED,
        )
        logger.info(
            "customer batch cancelled batch_id=%s telegram_chat_id=%s "
            "telegram_user_id=%s media_group_id=%s",
            batch["id"],
            batch.get("telegram_chat_id"),
            batch.get("telegram_user_id"),
            batch.get("media_group_id"),
        )

    await state.clear()
    await callback.message.answer(
        "Загрузка клиента отменена.\n\n"
        "Полученные документы сохранены в системе и не потеряны."
    )
    await callback.answer()


async def _accept_batch_document(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    repository: CustomerUploadBatchRepository,
    allow_restore: bool,
) -> None:
    if message.from_user is None or message.chat is None:
        if allow_restore:
            raise SkipHandler()
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    data = await state.get_data()
    batch_id = data.get("batch_id")
    batch = None

    if batch_id is not None:
        batch = await repository.get_batch_by_id(int(batch_id))

    if (
        batch is None
        or batch.get("status") != CustomerUploadBatchStatus.COLLECTING
    ):
        batch = await repository.get_active_batch(chat_id, user_id)
        if batch is None or batch["status"] != CustomerUploadBatchStatus.COLLECTING:
            if allow_restore:
                raise SkipHandler()
            await message.answer(
                "Активный пакет документов не найден.\n"
                "Начните заново командой /add_customer"
            )
            return
        await state.set_state(CustomerBatchUploadStates.collecting_documents)
        await state.update_data(batch_id=batch["id"])
        logger.info(
            "customer batch restored batch_id=%s telegram_chat_id=%s "
            "telegram_user_id=%s media_group_id=%s",
            batch["id"],
            chat_id,
            user_id,
            batch.get("media_group_id"),
        )

    media_group_id = message.media_group_id
    if media_group_id and not batch.get("media_group_id"):
        batch = await repository.set_batch_media_group_id(batch["id"], media_group_id)

    try:
        downloaded = await download_customer_file_from_telegram(
            message,
            bot,
            max_file_bytes=settings.customer_upload_max_file_bytes,
        )
    except CustomerFileDownloadError as exc:
        await message.answer(exc.user_message)
        return

    before_count = await repository.count_batch_files(batch["id"])
    await repository.add_file(
        batch_id=batch["id"],
        telegram_message_id=downloaded.telegram_message_id,
        telegram_file_id=downloaded.telegram_file_id,
        original_filename=downloaded.original_filename,
        mime_type=downloaded.mime_type,
        file_extension=downloaded.file_extension,
        file_size=downloaded.file_size,
        temporary_content=downloaded.content,
    )
    after_count = await repository.count_batch_files(batch["id"])

    if after_count == before_count:
        logger.info(
            "customer batch duplicate update ignored batch_id=%s "
            "telegram_chat_id=%s telegram_user_id=%s telegram_message_id=%s "
            "media_group_id=%s file_count=%s",
            batch["id"],
            chat_id,
            user_id,
            downloaded.telegram_message_id,
            media_group_id or batch.get("media_group_id"),
            after_count,
        )
        return

    logger.info(
        "customer batch file added batch_id=%s telegram_chat_id=%s "
        "telegram_user_id=%s telegram_message_id=%s media_group_id=%s "
        "file_count=%s",
        batch["id"],
        chat_id,
        user_id,
        downloaded.telegram_message_id,
        media_group_id or batch.get("media_group_id"),
        after_count,
    )
    await message.answer(
        _format_file_accepted_message(after_count),
        reply_markup=_batch_keyboard(after_count),
    )


async def _resolve_batch_for_callback(
    *,
    callback: CallbackQuery,
    state: FSMContext,
    repository: CustomerUploadBatchRepository,
) -> dict | None:
    data = await state.get_data()
    batch_id = data.get("batch_id")
    if batch_id is not None:
        batch = await repository.get_batch_by_id(int(batch_id))
        if batch is not None:
            return batch

    if callback.message is None or callback.from_user is None:
        return None

    return await repository.get_resumable_batch(
        callback.message.chat.id,
        callback.from_user.id,
    )


def _batch_keyboard(file_count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if file_count >= REQUIRED_DOCUMENTS_COUNT:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Обработать документы",
                    callback_data="customer_batch:process",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить загрузку",
                callback_data="customer_batch:cancel",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _recognition_retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Повторить распознавание",
                    callback_data="customer_batch:retry_recognition",
                )
            ]
        ]
    )


def _format_file_accepted_message(file_count: int) -> str:
    if file_count < REQUIRED_DOCUMENTS_COUNT:
        return (
            "Файл принят.\n\n"
            f"Получено документов: {file_count} из {REQUIRED_DOCUMENTS_COUNT}"
        )
    return (
        "Файл принят.\n\n"
        f"Получено документов: {file_count}\n\n"
        "Теперь можно запустить обработку."
    )


def _format_insufficient_files_message(file_count: int) -> str:
    if file_count == 1:
        loaded = "Загружен только 1 документ."
    elif file_count in {2, 3, 4}:
        loaded = f"Загружено только {file_count} документа."
    else:
        loaded = f"Загружено только {file_count} документов."
    return f"{loaded}\n\nДобавьте недостающие файлы."
