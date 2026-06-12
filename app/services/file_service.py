from __future__ import annotations

from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.types import Document


SUPPORTED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp", "heic", "docx", "xlsx"}


def get_original_filename(document: Document) -> str:
    return document.file_name or f"{document.file_unique_id}.bin"


def get_file_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix.removeprefix(".")


def is_supported_file(filename: str) -> bool:
    return get_file_extension(filename) in SUPPORTED_EXTENSIONS


def build_stored_filename(message_id: int, filename: str) -> str:
    safe_name = sanitize_filename(filename)
    return f"{message_id}_{safe_name}"


def sanitize_filename(filename: str) -> str:
    sanitized = "".join(
        char if char.isalnum() or char in (" ", ".", "_", "-") else "_"
        for char in filename
    ).strip()
    return sanitized or "document"


async def download_telegram_document(bot: Bot, document: Document) -> bytes:
    telegram_file = await bot.get_file(document.file_id)
    if telegram_file.file_path is None:
        raise RuntimeError("Telegram did not return a file path")

    buffer = BytesIO()
    await bot.download_file(telegram_file.file_path, destination=buffer)
    return buffer.getvalue()
