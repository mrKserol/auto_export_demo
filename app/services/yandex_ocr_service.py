from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import fitz
from PIL import Image

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    register_heif_opener = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

OCR_ENDPOINT = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
DEFAULT_OCR_MIN_DELAY_SECONDS = 1.5
DEFAULT_MAX_OCR_RETRIES = 5
PDF_RENDER_DPI = 200

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
_MIME_BY_EXTENSION = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "pdf": "application/pdf",
}


@dataclass(frozen=True)
class YandexOCRService:
    api_key: str
    min_delay_seconds: float = DEFAULT_OCR_MIN_DELAY_SECONDS
    max_retries: int = DEFAULT_MAX_OCR_RETRIES

    async def recognize_text(
        self,
        file_content: bytes,
        mime_type: str | None = None,
        *,
        filename: str | None = None,
    ) -> str:
        resolved_mime = _resolve_mime_type(file_content, mime_type, filename)
        extension = _extension_from_mime_or_filename(resolved_mime, filename)

        if extension == "pdf":
            page_texts = await self._recognize_pdf(file_content)
            return "\n\n".join(text for text in page_texts if text).strip()

        image_bytes, ocr_mime = await asyncio.to_thread(
            _prepare_image_bytes,
            file_content,
            extension,
            resolved_mime,
        )
        return await self._recognize_image(image_bytes, ocr_mime)

    async def _recognize_pdf(self, file_content: bytes) -> list[str]:
        page_images = await asyncio.to_thread(_render_pdf_pages, file_content)
        texts: list[str] = []
        for image_bytes in page_images:
            page_text = await self._recognize_image(image_bytes, "image/jpeg")
            if page_text:
                texts.append(page_text)
        return texts

    async def _recognize_image(self, image_bytes: bytes, mime_type: str) -> str:
        payload = {
            "mimeType": mime_type,
            "languageCodes": ["ru", "en"],
            "model": "page",
            "content": base64.b64encode(image_bytes).decode("ascii"),
        }
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(self.max_retries):
                async with session.post(OCR_ENDPOINT, json=payload, headers=headers) as response:
                    if response.status == 429 and attempt < self.max_retries - 1:
                        delay = self.min_delay_seconds * (attempt + 1)
                        logger.warning(
                            "Yandex OCR rate limited, retry %s/%s in %.1fs",
                            attempt + 1,
                            self.max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    if response.status >= 400:
                        error_body = await response.text()
                        logger.error(
                            "Yandex OCR failed with status %s: %s",
                            response.status,
                            _short_error(error_body),
                        )
                        raise RuntimeError(
                            f"Yandex OCR error {response.status}: {_short_error(error_body)}"
                        )

                    response_json = await response.json()
                    return _extract_ocr_text(response_json)

        raise RuntimeError("Yandex OCR retries exhausted")


def _resolve_mime_type(
    file_content: bytes,
    mime_type: str | None,
    filename: str | None,
) -> str:
    if mime_type:
        return mime_type.split(";", 1)[0].strip().lower()

    if filename:
        extension = Path(filename).suffix.lower().removeprefix(".")
        if extension in _MIME_BY_EXTENSION:
            return _MIME_BY_EXTENSION[extension]

    if file_content.startswith(b"%PDF"):
        return "application/pdf"

    return "application/octet-stream"


def _extension_from_mime_or_filename(mime_type: str, filename: str | None) -> str:
    for extension, mapped_mime in _MIME_BY_EXTENSION.items():
        if mime_type == mapped_mime:
            return extension

    if filename:
        extension = Path(filename).suffix.lower().removeprefix(".")
        if extension:
            return extension

    if mime_type == "application/pdf":
        return "pdf"
    if mime_type in {"image/jpeg", "image/jpg"}:
        return "jpeg"
    if mime_type == "image/png":
        return "png"
    if mime_type == "image/webp":
        return "webp"
    if mime_type in {"image/heic", "image/heif"}:
        return "heic"

    raise ValueError(f"Unsupported mime type for OCR: {mime_type}")


def _prepare_image_bytes(
    file_content: bytes,
    extension: str,
    mime_type: str,
) -> tuple[bytes, str]:
    if extension in {"jpg", "jpeg"} or mime_type in {"image/jpeg", "image/jpg"}:
        return file_content, "image/jpeg"
    if extension == "png" or mime_type == "image/png":
        return file_content, "image/png"
    if extension in {"heic", "heif"} or mime_type in {"image/heic", "image/heif"}:
        if register_heif_opener is None:
            raise RuntimeError("HEIC support requires pillow-heif")
        image = Image.open(io.BytesIO(file_content))
        return _image_to_jpeg_bytes(image)
    if extension == "webp" or mime_type == "image/webp":
        image = Image.open(io.BytesIO(file_content)).convert("RGB")
        return _image_to_jpeg_bytes(image)

    raise ValueError(f"Unsupported image format: {extension or mime_type}")


def _image_to_jpeg_bytes(image: Image.Image) -> tuple[bytes, str]:
    rgb_image = image.convert("RGB")
    buffer = io.BytesIO()
    rgb_image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue(), "image/jpeg"


def _render_pdf_pages(file_content: bytes) -> list[bytes]:
    document = fitz.open(stream=file_content, filetype="pdf")
    pages: list[bytes] = []
    try:
        for page in document:
            pixmap = page.get_pixmap(dpi=PDF_RENDER_DPI)
            pages.append(pixmap.tobytes("jpeg"))
    finally:
        document.close()
    return pages


def _extract_ocr_text(response_json: dict) -> str:
    result = response_json.get("result") or {}
    text_annotation = result.get("textAnnotation") or {}
    return str(text_annotation.get("fullText") or "").strip()


def _short_error(error_body: str, limit: int = 200) -> str:
    compact = re.sub(r"\s+", " ", error_body).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
