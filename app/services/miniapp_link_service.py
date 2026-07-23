from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import Settings
from app.web.token_service import create_specification_context_token


def build_specification_miniapp_url(settings: Settings, token: str) -> str:
    return f"{settings.mini_app_base_url}/miniapp/specification?token={token}"


def create_customer_specification_token(
    settings: Settings,
    *,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
) -> str:
    return create_specification_context_token(
        secret=settings.mini_app_token_secret,
        customer_id=customer_id,
        telegram_user_id=telegram_user_id,
        origin_chat_id=origin_chat_id,
        ttl_seconds=settings.mini_app_token_ttl_seconds,
    )


def build_miniapp_open_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Открыть форму спецификации",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )


def build_deep_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Заполнить спецификацию",
                    url=url,
                )
            ]
        ]
    )


def build_specification_deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=spec_{token}"
