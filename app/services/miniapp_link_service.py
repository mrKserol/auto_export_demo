from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import Settings
from app.web.token_service import (
    create_estimate_context_token,
    create_specification_context_token,
    create_specification_edit_context_token,
    create_customer_edit_context_token,
)


def build_specification_miniapp_url(settings: Settings, token: str) -> str:
    return f"{settings.mini_app_base_url}/miniapp/specification?token={token}"


def build_estimate_miniapp_url(settings: Settings, token: str) -> str:
    return f"{settings.mini_app_base_url}/miniapp/estimate?token={token}"


def create_customer_estimate_token(
    settings: Settings,
    *,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
) -> str:
    return create_estimate_context_token(
        secret=settings.mini_app_token_secret,
        customer_id=customer_id,
        telegram_user_id=telegram_user_id,
        origin_chat_id=origin_chat_id,
        ttl_seconds=settings.mini_app_token_ttl_seconds,
    )


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


def build_specification_edit_miniapp_url(settings: Settings, token: str) -> str:
    return f"{settings.mini_app_base_url}/miniapp/specification?token={token}&mode=edit"


def create_customer_specification_edit_token(
    settings: Settings,
    *,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
) -> str:
    return create_specification_edit_context_token(
        secret=settings.mini_app_token_secret,
        customer_id=customer_id,
        telegram_user_id=telegram_user_id,
        origin_chat_id=origin_chat_id,
        ttl_seconds=settings.mini_app_token_ttl_seconds,
    )


def build_specification_edit_open_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Открыть спецификацию", web_app=WebAppInfo(url=url))]
        ]
    )


def build_customer_edit_miniapp_url(settings: Settings, token: str) -> str:
    return f"{settings.mini_app_base_url}/miniapp/customer?token={token}"


def create_customer_edit_token(
    settings: Settings,
    *,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
) -> str:
    return create_customer_edit_context_token(
        secret=settings.mini_app_token_secret,
        customer_id=customer_id,
        telegram_user_id=telegram_user_id,
        origin_chat_id=origin_chat_id,
        ttl_seconds=settings.mini_app_token_ttl_seconds,
    )


def build_customer_edit_open_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Открыть данные клиента",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )
