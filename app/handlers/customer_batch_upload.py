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
    WebAppInfo,
)

from app.config import Settings
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_data_service import (
    assemble_customer_data_from_batch_files,
    format_customer_data_preview,
)
from app.services.customer_batch_recognition_service import (
    CustomerBatchRecognitionService,
    format_kit_telegram_message,
    validate_document_kit,
)
from app.services.customer_file_download_service import (
    CustomerFileDownloadError,
    download_customer_file_from_telegram,
)
from app.services.customer_folder_service import CustomerFolderService
from app.services.miniapp_link_service import (
    build_customer_edit_miniapp_url,
    create_customer_batch_token,
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
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
    customer_batch_recognition_service: CustomerBatchRecognitionService,
    customer_folder_service: CustomerFolderService,
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

    if status == CustomerUploadBatchStatus.CUSTOMER_SAVED:
        await callback.message.answer("Этот пакет уже сохранён как клиент.")
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
    await callback.answer()

    need_ocr = status in {
        CustomerUploadBatchStatus.COLLECTING,
        CustomerUploadBatchStatus.RECOGNIZING,
        CustomerUploadBatchStatus.FAILED,
    }
    recognition_result = None
    if need_ocr:
        await callback.message.answer(
            "Распознаю документы.\n"
            f"Получено файлов: {file_count}\n\n"
            "Это может занять некоторое время."
        )
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
            recognition_result = await customer_batch_recognition_service.process_batch(
                batch["id"]
            )
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            logger.exception(
                "customer batch recognition handler failed batch_id=%s",
                batch["id"],
            )
            await callback.message.answer(
                "Не удалось завершить распознавание документов.\n\n"
                "Полученные файлы сохранены. Попробуйте запустить обработку повторно.",
                reply_markup=_post_recognition_keyboard(include_edit=False),
            )
            return

        if recognition_result.is_technical_failure:
            await callback.message.answer(
                recognition_result.telegram_message,
                reply_markup=_post_recognition_keyboard(include_edit=False),
            )
            return

    try:
        await bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_DOCUMENT)
        finalize = await customer_folder_service.ensure_batch_files_saved(batch["id"])
    except Exception:
        logger.exception(
            "customer batch folder finalize failed batch_id=%s",
            batch["id"],
        )
        await callback.message.answer(
            "Документы распознаны, но не удалось сохранить файлы на Яндекс Диск.\n"
            "Попробуйте повторить обработку.",
            reply_markup=_post_recognition_keyboard(include_edit=True),
        )
        return

    files = await customer_upload_batch_repository.get_batch_files(batch["id"])
    assembled = assemble_customer_data_from_batch_files(
        files,
        kit_warnings=(
            recognition_result.kit.warnings if recognition_result is not None else None
        ),
    )
    kit_message = (
        recognition_result.telegram_message
        if recognition_result is not None
        else format_kit_telegram_message(validate_document_kit(files))
    )
    preview = format_customer_data_preview(
        assembled,
        customer_path=finalize.customer_path,
        kit_message=kit_message,
    )
    if finalize.failed_count:
        preview += (
            "\n\nЧасть файлов не удалось загрузить на Яндекс Диск. "
            "Папка сохранена, повторите обработку для дозагрузки."
        )

    await callback.message.answer(
        preview,
        reply_markup=_post_recognition_keyboard(include_edit=True),
    )


@router.callback_query(F.data == "customer_batch:edit")
async def handle_batch_edit(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
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
    if batch is None:
        await callback.message.answer(
            "Активный пакет документов не найден.\n"
            "Начните заново командой /add_customer"
        )
        await callback.answer()
        return

    if batch["status"] in {
        CustomerUploadBatchStatus.ABANDONED,
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
    }:
        await callback.message.answer("Этот пакет уже недоступен для редактирования.")
        await callback.answer()
        return

    token = create_customer_batch_token(
        settings,
        batch_id=int(batch["id"]),
        telegram_user_id=callback.from_user.id,
        origin_chat_id=callback.message.chat.id,
    )
    url = build_customer_edit_miniapp_url(settings, token)
    if batch["status"] not in {
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
    }:
        await customer_upload_batch_repository.mark_batch_awaiting_confirmation(
            int(batch["id"])
        )
    await state.update_data(batch_id=batch["id"])
    await callback.message.answer(
        "Откройте форму «Данные клиента» и проверьте распознанные поля.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📝 Ручная коррекция",
                        web_app=WebAppInfo(url=url),
                    )
                ]
            ]
        ),
    )
    await callback.answer()


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
    if batch is not None and batch["status"] not in {
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
        CustomerUploadBatchStatus.ABANDONED,
    }:
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
    return _post_recognition_keyboard(include_edit=False)


def _post_recognition_keyboard(*, include_edit: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if include_edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📝 Ручная коррекция",
                    callback_data="customer_batch:edit",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Повторить распознавание",
                callback_data="customer_batch:retry_recognition",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data="customer_batch:cancel",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
