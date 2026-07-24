from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import Settings
from app.database import Database

logger = logging.getLogger(__name__)

DeliveryChannel = Literal["private", "dm", "deep_link"]

PURPOSE_EDIT_BATCH = "edit_batch"
PURPOSE_SPECIFICATION = "spec"
PURPOSE_CUSTOMER_EDIT = "cus"
PURPOSE_ESTIMATE = "est"

AnswerCallable = Callable[..., Awaitable[object]]


@dataclass(frozen=True)
class MiniAppEntrypointResult:
    channel: DeliveryChannel
    launch_code: str | None = None


def build_web_app_keyboard(button_text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button_text, web_app=WebAppInfo(url=url))]
        ]
    )


def build_open_bot_keyboard(url: str, *, button_text: str = "Открыть бота") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=url)]]
    )


def build_purpose_deep_link(bot_username: str, purpose: str, launch_code: str) -> str:
    return f"https://t.me/{bot_username}?start={purpose}_{launch_code}"


def is_private_chat(chat_type: str | None) -> bool:
    return (chat_type or "") == "private"


def is_group_chat(chat_type: str | None) -> bool:
    return (chat_type or "") in {"group", "supergroup"}


async def send_miniapp_entrypoint(
    bot: Bot,
    *,
    database: Database,
    settings: Settings,
    user_id: int,
    origin_chat_type: str,
    answer: AnswerCallable,
    purpose: str,
    entity_id: int,
    context_token: str,
    miniapp_url: str,
    open_button_text: str,
    private_text: str,
    group_success_text: str,
    deep_link_group_text: str,
    customer_id: int | None = None,
    ttl_seconds: int | None = None,
    deep_link_button_text: str = "Открыть бота",
) -> MiniAppEntrypointResult:
    """Deliver a Mini App entrypoint safely for private and group chats.

    Web App buttons are only sent in private chats (origin private or DM).
    Group/supergroup never receive web_app buttons.
    """
    _ = entity_id  # reserved for logging / future audit; identity lives in context_token
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.mini_app_token_ttl_seconds)
    keyboard = build_web_app_keyboard(open_button_text, miniapp_url)

    if is_private_chat(origin_chat_type):
        await answer(private_text, reply_markup=keyboard)
        return MiniAppEntrypointResult(channel="private")

    me = await bot.get_me()
    bot_username = me.username or ""
    launch_code = await database.create_mini_app_launch_code(
        context_token=context_token,
        telegram_user_id=user_id,
        customer_id=customer_id,
        purpose=purpose,
        ttl_seconds=ttl,
    )
    deep_link = build_purpose_deep_link(bot_username, purpose, launch_code)

    try:
        await bot.send_message(
            chat_id=user_id,
            text=private_text,
            reply_markup=keyboard,
        )
        await answer(group_success_text)
        logger.info(
            "Mini App entrypoint delivered via DM purpose=%s user_id=%s",
            purpose,
            user_id,
        )
        return MiniAppEntrypointResult(channel="dm", launch_code=launch_code)
    except (TelegramForbiddenError, TelegramBadRequest) as error:
        logger.info(
            "Cannot DM user for Mini App purpose=%s user_id=%s error=%s",
            purpose,
            user_id,
            error,
        )
        await answer(
            deep_link_group_text,
            reply_markup=build_open_bot_keyboard(
                deep_link,
                button_text=deep_link_button_text,
            ),
        )
        return MiniAppEntrypointResult(channel="deep_link", launch_code=launch_code)
