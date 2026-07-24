from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging

from aiogram import Bot
from aiogram.types import Message

from app.services.file_service import get_file_extension, get_original_filename


logger = logging.getLogger(__name__)

CUSTOMER_UPLOAD_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp", "heic", "heif"}


class CustomerFileDownloadError(Exception):
    """Raised when a customer upload cannot be downloaded or validated."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class DownloadedCustomerFile:
    telegram_file_id: str
    telegram_message_id: int
    original_filename: str
    mime_type: str | None
    file_extension: str
    file_size: int | None
    content: bytes


async def download_customer_file_from_telegram(
    message: Message,
    bot: Bot,
    *,
    max_file_bytes: int,
) -> DownloadedCustomerFile:
    """Download a photo/document from Telegram without uploading to Yandex Disk."""
    if message.document is not None:
        document = message.document
        telegram_file_id = document.file_id
        original_filename = get_original_filename(document)
        mime_type = document.mime_type
        declared_size = document.file_size
    elif message.photo:
        photo = message.photo[-1]
        telegram_file_id = photo.file_id
        original_filename = f"photo_{message.message_id}.jpg"
        mime_type = "image/jpeg"
        declared_size = photo.file_size
    else:
        raise CustomerFileDownloadError(
            "Отправьте фотографию или PDF-файл документа."
        )

    extension = get_file_extension(original_filename)
    if extension not in CUSTOMER_UPLOAD_EXTENSIONS:
        raise CustomerFileDownloadError(
            "Формат файла не поддерживается. Поддерживаются: "
            "pdf, jpg, jpeg, png, webp, heic, heif."
        )

    if declared_size is not None and declared_size > max_file_bytes:
        raise CustomerFileDownloadError(
            _format_file_too_large_message(max_file_bytes)
        )

    try:
        telegram_file = await bot.get_file(telegram_file_id)
        if telegram_file.file_path is None:
            raise RuntimeError("Telegram did not return a file path")
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        content = buffer.getvalue()
    except CustomerFileDownloadError:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to download customer file from Telegram message_id=%s",
            message.message_id,
        )
        raise CustomerFileDownloadError(
            "Не удалось скачать файл из Telegram."
        ) from exc

    if len(content) > max_file_bytes:
        raise CustomerFileDownloadError(
            _format_file_too_large_message(max_file_bytes)
        )

    return DownloadedCustomerFile(
        telegram_file_id=telegram_file_id,
        telegram_message_id=message.message_id,
        original_filename=original_filename,
        mime_type=mime_type,
        file_extension=extension,
        file_size=declared_size if declared_size is not None else len(content),
        content=content,
    )


def _format_file_too_large_message(max_file_bytes: int) -> str:
    max_mb = max(1, max_file_bytes // (1024 * 1024))
    return (
        f"Файл слишком большой.\n"
        f"Максимальный размер: {max_mb} МБ."
    )
