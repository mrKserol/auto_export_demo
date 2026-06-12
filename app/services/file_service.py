from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import unicodedata

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
    normalized_name = normalize_stored_filename(filename)
    return f"{message_id}_{normalized_name}"


def normalize_stored_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFC", filename).strip()
    suffix = Path(normalized).suffix

    if suffix:
        extension = suffix.removeprefix(".").lower()
        stem = normalized[: -len(suffix)].rstrip()
    else:
        extension = ""
        stem = normalized

    stem = re.sub(r"\s+", "_", stem.strip())
    stem = _sanitize_filename_part(stem) or "document"

    if not extension:
        return stem

    extension = _sanitize_filename_part(extension.lower())
    return f"{stem}.{extension}"


def _sanitize_filename_part(value: str) -> str:
    sanitized = "".join(
        char if char.isalnum() or char in (".", "_", "-") else "_"
        for char in value
    )
    return sanitized.strip("._-")


async def download_telegram_document(bot: Bot, document: Document) -> bytes:
    telegram_file = await bot.get_file(document.file_id)
    if telegram_file.file_path is None:
        raise RuntimeError("Telegram did not return a file path")

    buffer = BytesIO()
    await bot.download_file(telegram_file.file_path, destination=buffer)
    return buffer.getvalue()
