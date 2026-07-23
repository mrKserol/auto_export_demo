from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.database import Database
from app.services.miniapp_link_service import (
    build_miniapp_open_keyboard,
    build_specification_miniapp_url,
    create_customer_specification_token,
)
from app.web.token_service import TokenError, verify_specification_context_token

router = Router(name="start")
logger = logging.getLogger(__name__)

START_MENU_TEXT = (
    "Команды:\n"
    "- /add_customer — добавить клиента по паспорту, СНИЛС, ИНН\n"
    "- /add_specification — добавить желаемый автомобиль клиента\n"
    "- /search_edit_customer — найти или изменить клиента"
)

INVALID_SPEC_LINK_TEXT = (
    "Ссылка недействительна или устарела. "
    "Вернитесь к карточке клиента и откройте форму заново."
)


@router.message(CommandStart())
async def handle_start(
    message: Message,
    command: CommandObject,
    settings: Settings,
    database: Database,
    bot: Bot,
) -> None:
    args = (command.args or "").strip()
    if args.startswith("spec_"):
        await _handle_specification_deep_link(
            message=message,
            launch_code=args[len("spec_") :],
            settings=settings,
            database=database,
        )
        return

    await message.answer(START_MENU_TEXT)


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
