from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from uuid import UUID

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.config import Settings
from app.database import Database
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import CustomerUploadBatchStatus
from app.services.n8n_chat_service import (
    N8NChatError,
    N8NChatService,
    n8n_chat_configured,
)
from app.services.document_intake_service import DocumentIntakeError, DocumentIntakeService
from app.services.channel_action_token import verify_channel_action_token
from app.services.channel_action_token import create_channel_action_token
from app.services.intake_customer_service import IntakeCustomerService
from app.services.miniapp_link_service import create_intake_review_token
from app.services.compact_action_token import (
    consume_compact_action_token,
    create_compact_action_token,
    telegram_callback_data,
    consume_compact_action_token_db,
    create_compact_action_token_db,
)

router = Router(name="n8n_chat")
logger = logging.getLogger(__name__)

N8N_UNAVAILABLE_USER_TEXT = (
    "ИИ-ассистент временно недоступен. Попробуйте позже."
)
N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT = (
    "Этот файл пока не поддерживается. Пришлите фото или PDF."
)
SUPPORTED_DOCUMENT_MIME_TYPES = {"application/pdf"}
INTAKE_COMMANDS = ("intake_start", "intake_finish", "intake_cancel")


def _durable_token_store(database) -> bool:
    return isinstance(database, Database)


@router.callback_query(F.data.startswith("a:") | F.data.startswith("intake.action:") | F.data.startswith("intake.add_client:"))
async def handle_intake_add_client_callback(
    callback: CallbackQuery,
    settings: Settings,
    intake_service: DocumentIntakeService,
    intake_customer_service: IntakeCustomerService | None = None,
    database=None,
) -> None:
    callback_data = callback.data or ""
    try:
        if callback_data.startswith("a:"):
            if callback.from_user is None or callback.message is None:
                raise ValueError
            if _durable_token_store(database):
                record = await consume_compact_action_token_db(
                    database=database, token=callback_data[2:], action=None,
                    channel="telegram", external_user_id=str(callback.from_user.id),
                    conversation_id=str(callback.message.chat.id),
                )
                session_id = UUID(str(record["session_id"]))
            else:
                record = consume_compact_action_token(
                    callback_data[2:], action=None, channel="telegram",
                    external_user_id=str(callback.from_user.id),
                    conversation_id=str(callback.message.chat.id),
                )
                session_id = record.session_id
            action = record
        elif callback_data.startswith("intake.action:"):
            action = verify_channel_action_token(
                callback_data.split(":", 1)[1],
                secret=settings.mini_app_token_secret,
                expected_channel="telegram",
            )
            if action.action not in {"intake.add_client", "intake.replace_client", "intake.cancel"}:
                raise ValueError
            session_id = UUID(action.session_id)
        else:
            # Read legacy callbacks from cards created before action tokens.
            session_id = UUID(callback_data.split(":", 1)[-1])
        if callback.from_user is None or callback.message is None:
            raise ValueError
        await intake_service.validate_session_owner(
            session_id,
            channel="telegram",
            external_user_id=str(callback.from_user.id),
            conversation_id=str(callback.message.chat.id),
        )
    except (ValueError, DocumentIntakeError):
        await callback.answer("Нет доступа к этой intake-сессии", show_alert=True)
        return

    if intake_customer_service is None:
        await callback.answer("Проверяю клиента по паспорту…")
        return
    try:
        action_name = action["action"] if isinstance(action, dict) else action.action
        if callback_data.startswith("a:") and action_name == "intake.add_client":
            result = await intake_customer_service.add_client(
                session_id=session_id, channel="telegram",
                external_user_id=str(callback.from_user.id),
                conversation_id=str(callback.message.chat.id),
            )
        elif (callback_data.startswith("a:") or callback_data.startswith("intake.action:")) and action_name == "intake.replace_client":
            result = await intake_customer_service.replace_client(
                session_id=session_id, customer_id=int(action.get("customer_id") if isinstance(action, dict) else action.customer_id),
                channel="telegram", external_user_id=str(callback.from_user.id),
                conversation_id=str(callback.message.chat.id),
            )
        elif (callback_data.startswith("a:") or callback_data.startswith("intake.action:")) and action_name == "intake.cancel":
            summary = await intake_service.get_review_summary(session_id)
            review_token = create_intake_review_token(
                settings, session_id=str(session_id), telegram_user_id=callback.from_user.id,
                origin_chat_id=callback.message.chat.id,
            )
            if _durable_token_store(database):
                add_token = await create_compact_action_token_db(
                    database=database, action="intake.add_client", session_id=session_id,
                    channel="telegram", external_user_id=str(callback.from_user.id),
                    conversation_id=str(callback.message.chat.id),
                )
            else:
                add_token = create_compact_action_token(
                    action="intake.add_client", session_id=session_id, channel="telegram",
                    external_user_id=str(callback.from_user.id), conversation_id=str(callback.message.chat.id),
                )
            await callback.message.edit_text(
                intake_service.build_intake_review_card(summary),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Ручная коррекция", web_app=WebAppInfo(url=f"{settings.mini_app_base_url}/miniapp/customer?token={review_token}"))],
                    [InlineKeyboardButton(text="➕ Добавить клиента", callback_data=telegram_callback_data(add_token))],
                ]),
            )
            await callback.answer("Отменено")
            return
        else:
            result = await intake_customer_service.add_client(
                session_id=session_id, channel="telegram",
                external_user_id=str(callback.from_user.id),
                conversation_id=str(callback.message.chat.id),
            )
    except Exception:
        logger.exception("intake customer action failed")
        await callback.answer("Не удалось обработать действие", show_alert=True)
        return
    await callback.answer()
    if result.status == "missing_passport":
        await callback.message.edit_text(result.message or "Заполните номер паспорта в ручной коррекции")
    elif result.status == "duplicate":
        secret = settings.mini_app_token_secret
        token_factory = create_compact_action_token_db if _durable_token_store(database) else create_compact_action_token
        token_kwargs = dict(session_id=session_id, channel="telegram",
                            external_user_id=str(callback.from_user.id),
                            conversation_id=str(callback.message.chat.id))
        if _durable_token_store(database):
            replace_raw = await token_factory(database=database, action="intake.replace_client", **token_kwargs, customer_id=str(result.customer["id"]))
            cancel_raw = await token_factory(database=database, action="intake.cancel", **token_kwargs)
        else:
            replace_raw = token_factory(action="intake.replace_client", **token_kwargs, customer_id=str(result.customer["id"]))
            cancel_raw = token_factory(action="intake.cancel", **token_kwargs)
        replace_token = telegram_callback_data(replace_raw)
        cancel_token = telegram_callback_data(cancel_raw)
        await callback.message.edit_text(
            result.message or "Клиент с таким паспортом уже существует.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Заменить", callback_data=replace_token)],
                [InlineKeyboardButton(text="Отмена", callback_data=cancel_token)],
            ]),
        )
    elif result.status in {"completed", "replaced"}:
        customer = result.customer or {}
        await callback.message.edit_text(
            ("ДАННЫЕ КЛИЕНТА ОБНОВЛЕНЫ" if result.status == "replaced" else "КЛИЕНТ ДОБАВЛЕН") + "\n\n"
            f"ФИО: {customer.get('surname') or 'не распознано'} {customer.get('first_name') or ''}\n"
            f"Паспорт: {customer.get('passport') or 'не распознано'}\n"
            f"Телефон: {customer.get('phone') or 'не распознано'}\n"
            f"Email: {customer.get('email') or 'не распознано'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Редактировать клиента", callback_data=f"customer_edit:{customer['id']}")],
                [InlineKeyboardButton(text="📝 Добавить спецификацию", callback_data=f"customer_edit_spec:{customer['id']}")],
            ]),
        )


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    Command(*INTAKE_COMMANDS),
)
async def handle_n8n_intake_command(message: Message, settings: Settings, intake_service: DocumentIntakeService | None = None, database=None) -> None:
    if not n8n_chat_configured(settings):
        return

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_command(message)
    except N8NChatError:
        logger.exception("n8n intake command webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    if isinstance(reply, dict) and reply.get("type") == "intake_card":
        keyboard = []
        for button in reply.get("keyboard", []):
            if not isinstance(button, dict):
                continue
            if button.get("type") == "web_app" and isinstance(button.get("url"), str):
                keyboard.append([InlineKeyboardButton(text=str(button.get("text") or ""), web_app=WebAppInfo(url=button["url"]))])
            elif button.get("type") == "callback":
                if _durable_token_store(database):
                    action_token = await create_compact_action_token_db(
                        database=database, action=str(button.get("action") or "intake.add_client"),
                        session_id=reply["session_id"], channel="telegram",
                        external_user_id=str(message.from_user.id if message.from_user else ""),
                        conversation_id=str(message.chat.id),
                    )
                else:
                    action_token = create_compact_action_token(
                        action=str(button.get("action") or "intake.add_client"),
                        session_id=reply["session_id"], channel="telegram",
                        external_user_id=str(message.from_user.id if message.from_user else ""),
                        conversation_id=str(message.chat.id),
                    )
                keyboard.append([InlineKeyboardButton(text=str(button.get("text") or ""), callback_data=telegram_callback_data(action_token))])
        try:
            sent = await message.answer(reply["text"], reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        except TelegramBadRequest:
            logger.warning("Telegram rejected intake keyboard (callback data validation)")
            await message.answer("Не удалось показать кнопки intake. Попробуйте ещё раз.")
            return
        # The first card is the anchor PATCH later edits. Keep only neutral ref data.
        if reply.get("session_id") and intake_service is not None:
            try:
                from uuid import UUID
                await intake_service.set_review_card_ref(
                    UUID(reply["session_id"]), channel="telegram",
                    conversation_id=str(sent.chat.id), message_id=str(sent.message_id),
                )
            except Exception:
                logger.exception("Failed to persist intake review card reference")
        return
    if isinstance(reply, dict) and reply.get("type") == "web_app":
        await message.answer(
            reply["text"],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=reply["button_text"],
                            web_app=WebAppInfo(url=reply["url"]),
                        )
                    ]
                ]
            ),
        )
    else:
        await message.answer(str(reply))


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
)
async def handle_n8n_private_text(message: Message, settings: Settings) -> None:
    if not n8n_chat_configured(settings):
        return

    text = (message.text or "").strip()
    if not text:
        return

    chat_id = "" if message.chat is None else str(message.chat.id)
    user_id = "" if message.from_user is None else str(message.from_user.id)
    message_id = "" if message.message_id is None else str(message.message_id)

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_text(
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
    except N8NChatError:
        logger.exception("n8n chat webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.photo,
)
async def handle_n8n_private_photo(
    message: Message,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if not n8n_chat_configured(settings):
        return

    await _skip_if_active_customer_batch(message, customer_upload_batch_repository)

    if not message.photo:
        return

    photo = message.photo[-1]
    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_attachment(
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            name=f"photo_{message.message_id}.jpg",
            mime_type="image/jpeg",
            kind="photo",
            size=photo.file_size,
            chat_id="" if message.chat is None else str(message.chat.id),
            user_id="" if message.from_user is None else str(message.from_user.id),
            message_id="" if message.message_id is None else str(message.message_id),
            media_group_id=message.media_group_id,
            caption=message.caption,
        )
    except N8NChatError:
        logger.exception("n8n attachment webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.document,
)
async def handle_n8n_private_document(
    message: Message,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if not n8n_chat_configured(settings):
        return

    await _skip_if_active_customer_batch(message, customer_upload_batch_repository)

    document = message.document
    if document is None:
        return

    mime_type = document.mime_type or "application/octet-stream"
    if not (mime_type.startswith("image/") or mime_type in SUPPORTED_DOCUMENT_MIME_TYPES):
        await message.answer(N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT)
        return

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_attachment(
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            name=document.file_name or f"document_{message.message_id}",
            mime_type=mime_type,
            kind="document",
            size=document.file_size,
            chat_id="" if message.chat is None else str(message.chat.id),
            user_id="" if message.from_user is None else str(message.from_user.id),
            message_id="" if message.message_id is None else str(message.message_id),
            media_group_id=message.media_group_id,
            caption=message.caption,
        )
    except N8NChatError:
        logger.exception("n8n attachment webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)


async def _skip_if_active_customer_batch(
    message: Message,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if message.chat is None or message.from_user is None:
        return

    batch = await customer_upload_batch_repository.get_active_batch(
        message.chat.id,
        message.from_user.id,
    )
    if batch is not None and batch.get("status") == CustomerUploadBatchStatus.COLLECTING:
        raise SkipHandler()
