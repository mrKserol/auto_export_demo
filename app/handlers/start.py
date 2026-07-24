from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.database import Database
from app.services.miniapp_entrypoint_service import (
    PURPOSE_CUSTOMER_EDIT,
    PURPOSE_EDIT_BATCH,
    PURPOSE_ESTIMATE,
    PURPOSE_SPECIFICATION,
    build_web_app_keyboard,
)
from app.services.miniapp_link_service import (
    build_customer_edit_miniapp_url,
    build_estimate_miniapp_url,
    build_miniapp_open_keyboard,
    build_specification_miniapp_url,
    create_customer_batch_token,
    create_customer_edit_token,
    create_customer_estimate_token,
    create_customer_specification_token,
)
from app.web.token_service import (
    TokenError,
    verify_customer_batch_context_token,
    verify_customer_edit_context_token,
    verify_estimate_context_token,
    verify_specification_context_token,
)

router = Router(name="start")
logger = logging.getLogger(__name__)

START_MENU_TEXT = (
    "Команды:\n"
    "- /add_customer — добавить клиента по паспорту, СНИЛС, ИНН\n"
    "- /add_specification — добавить желаемый автомобиль клиента\n"
    "- /search_edit_customer — найти или изменить клиента"
)

INVALID_LINK_TEXT = (
    "Ссылка недействительна или устарела. "
    "Вернитесь к карточке клиента и откройте форму заново."
)

INVALID_SPEC_LINK_TEXT = INVALID_LINK_TEXT


@router.message(CommandStart())
async def handle_start(
    message: Message,
    command: CommandObject,
    settings: Settings,
    database: Database,
    bot: Bot,
) -> None:
    args = (command.args or "").strip()
    if args.startswith(f"{PURPOSE_EDIT_BATCH}_"):
        await _handle_edit_batch_deep_link(
            message=message,
            launch_code=args[len(f"{PURPOSE_EDIT_BATCH}_") :],
            settings=settings,
            database=database,
        )
        return
    if args.startswith(f"{PURPOSE_SPECIFICATION}_"):
        await _handle_specification_deep_link(
            message=message,
            launch_code=args[len(f"{PURPOSE_SPECIFICATION}_") :],
            settings=settings,
            database=database,
        )
        return
    if args.startswith(f"{PURPOSE_CUSTOMER_EDIT}_"):
        await _handle_customer_edit_deep_link(
            message=message,
            launch_code=args[len(f"{PURPOSE_CUSTOMER_EDIT}_") :],
            settings=settings,
            database=database,
        )
        return
    if args.startswith(f"{PURPOSE_ESTIMATE}_"):
        await _handle_estimate_deep_link(
            message=message,
            launch_code=args[len(f"{PURPOSE_ESTIMATE}_") :],
            settings=settings,
            database=database,
        )
        return

    await message.answer(START_MENU_TEXT)


async def _handle_edit_batch_deep_link(
    *,
    message: Message,
    launch_code: str,
    settings: Settings,
    database: Database,
) -> None:
    if message.from_user is None or not launch_code:
        await message.answer(INVALID_LINK_TEXT)
        return

    launch = await database.consume_mini_app_launch_code(
        launch_code,
        telegram_user_id=message.from_user.id,
        expected_purpose=PURPOSE_EDIT_BATCH,
    )
    if not launch:
        logger.warning(
            "Invalid or expired edit_batch launch code telegram_user_id=%s",
            message.from_user.id,
        )
        await message.answer(INVALID_LINK_TEXT)
        return

    try:
        context = verify_customer_batch_context_token(
            launch["context_token"],
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=message.from_user.id,
        )
    except TokenError:
        logger.warning(
            "Invalid batch context token from launch code telegram_user_id=%s",
            message.from_user.id,
        )
        await message.answer(INVALID_LINK_TEXT)
        return

    fresh_token = create_customer_batch_token(
        settings,
        batch_id=context.batch_id,
        telegram_user_id=message.from_user.id,
        origin_chat_id=message.chat.id,
    )
    url = build_customer_edit_miniapp_url(settings, fresh_token)
    await message.answer(
        "Откройте форму «Данные клиента» и проверьте распознанные поля.",
        reply_markup=build_web_app_keyboard("📝 Открыть данные клиента", url),
    )


async def _handle_specification_deep_link(
    *,
    message: Message,
    launch_code: str,
    settings: Settings,
    database: Database,
) -> None:
    if message.from_user is None or not launch_code:
        await message.answer(INVALID_SPEC_LINK_TEXT)
        return

    launch = await database.consume_mini_app_launch_code(
        launch_code,
        telegram_user_id=message.from_user.id,
    )
    if not launch:
        logger.warning(
            "Invalid or expired specification launch code telegram_user_id=%s",
            message.from_user.id,
        )
        await message.answer(INVALID_SPEC_LINK_TEXT)
        return
    purpose = launch.get("purpose")
    if purpose not in {None, "", PURPOSE_SPECIFICATION}:
        await message.answer(INVALID_SPEC_LINK_TEXT)
        return

    try:
        context = verify_specification_context_token(
            launch["context_token"],
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=message.from_user.id,
        )
    except TokenError:
        logger.warning(
            "Invalid specification context token from launch code "
            "telegram_user_id=%s",
            message.from_user.id,
        )
        await message.answer(INVALID_SPEC_LINK_TEXT)
        return

    customer = await database.get_customer_by_id(context.customer_id)
    if not customer:
        await message.answer("Клиент не найден")
        return

    if customer.get("specification_id"):
        await message.answer(
            "У клиента уже есть спецификация. Откройте карточку клиента и выберите редактирование."
        )
        return

    fresh_token = create_customer_specification_token(
        settings,
        customer_id=context.customer_id,
        telegram_user_id=message.from_user.id,
        origin_chat_id=message.chat.id,
    )
    url = build_specification_miniapp_url(settings, fresh_token)
    logger.info(
        "Opened specification mini app from deep-link customer_id=%s telegram_user_id=%s",
        context.customer_id,
        message.from_user.id,
    )
    await message.answer(
        "Откройте форму спецификации:",
        reply_markup=build_miniapp_open_keyboard(url),
    )


async def _handle_customer_edit_deep_link(
    *,
    message: Message,
    launch_code: str,
    settings: Settings,
    database: Database,
) -> None:
    if message.from_user is None or not launch_code:
        await message.answer(INVALID_LINK_TEXT)
        return

    launch = await database.consume_mini_app_launch_code(
        launch_code,
        telegram_user_id=message.from_user.id,
        expected_purpose=PURPOSE_CUSTOMER_EDIT,
    )
    if not launch:
        await message.answer(INVALID_LINK_TEXT)
        return

    try:
        context = verify_customer_edit_context_token(
            launch["context_token"],
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=message.from_user.id,
        )
    except TokenError:
        await message.answer(INVALID_LINK_TEXT)
        return

    fresh_token = create_customer_edit_token(
        settings,
        customer_id=context.customer_id,
        telegram_user_id=message.from_user.id,
        origin_chat_id=message.chat.id,
    )
    url = build_customer_edit_miniapp_url(settings, fresh_token)
    await message.answer(
        "Откройте форму с данными клиента:",
        reply_markup=build_web_app_keyboard("✏️ Открыть данные клиента", url),
    )


async def _handle_estimate_deep_link(
    *,
    message: Message,
    launch_code: str,
    settings: Settings,
    database: Database,
) -> None:
    if message.from_user is None or not launch_code:
        await message.answer(INVALID_LINK_TEXT)
        return

    launch = await database.consume_mini_app_launch_code(
        launch_code,
        telegram_user_id=message.from_user.id,
        expected_purpose=PURPOSE_ESTIMATE,
    )
    if not launch:
        await message.answer(INVALID_LINK_TEXT)
        return

    try:
        context = verify_estimate_context_token(
            launch["context_token"],
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=message.from_user.id,
        )
    except TokenError:
        await message.answer(INVALID_LINK_TEXT)
        return

    fresh_token = create_customer_estimate_token(
        settings,
        customer_id=context.customer_id,
        telegram_user_id=message.from_user.id,
        origin_chat_id=message.chat.id,
    )
    url = build_estimate_miniapp_url(settings, fresh_token)
    await message.answer(
        "Откройте форму сметы:",
        reply_markup=build_web_app_keyboard("📊 Открыть форму сметы", url),
    )
