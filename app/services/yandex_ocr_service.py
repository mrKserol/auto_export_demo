from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp
try:
    import fitz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - environment-dependent dependency
    fitz = None  # type: ignore[assignment]
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

    async def recognize_text_with_rotation_candidates(
        self,
        file_content: bytes,
        mime_type: str | None = None,
        *,
        filename: str | None = None,
        score_profile: str = "registration",
    ) -> str:
        resolved_mime = _resolve_mime_type(file_content, mime_type, filename)
        extension = _extension_from_mime_or_filename(resolved_mime, filename)

        if extension == "pdf":
            return await self.recognize_text(file_content, mime_type, filename=filename)

        if extension not in _IMAGE_EXTENSIONS:
            return await self.recognize_text(file_content, mime_type, filename=filename)

        candidates = await asyncio.to_thread(
            _prepare_rotated_image_bytes_candidates,
            file_content,
            extension,
            resolved_mime,
        )

        best_angle = 0
        best_text = ""
        best_score = float("-inf")
        for angle, image_bytes, ocr_mime in candidates:
            text = await self._recognize_image(image_bytes, ocr_mime)
            score = _score_ocr_text(text, score_profile)
            if score > best_score:
                best_score = score
                best_angle = angle
                best_text = text

        logger.info(
            "OCR rotation selected for profile=%s angle=%s text_length=%s score=%s",
            score_profile,
            best_angle,
            len(best_text),
            best_score,
        )
        return best_text

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
        last_error: Exception | None = None
        async with _OCR_REQUEST_LOCK:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(self.max_retries):
                    try:
                        async with session.post(
                            OCR_ENDPOINT,
                            json=payload,
                            headers=headers,
                        ) as response:
                            if (
                                response.status in _RETRYABLE_HTTP_STATUSES
                                and attempt < self.max_retries - 1
                            ):
                                delay = _retry_delay_seconds(attempt)
                                logger.warning(
                                    "Yandex OCR retryable status=%s attempt=%s/%s "
                                    "delay=%.1fs",
                                    response.status,
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
                                    f"Yandex OCR error {response.status}: "
                                    f"{_short_error(error_body)}"
                                )

                            response_json = await response.json()
                            return _extract_ocr_text(response_json)
                    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                        last_error = exc
                        if attempt >= self.max_retries - 1:
                            break
                        delay = _retry_delay_seconds(attempt)
                        logger.warning(
                            "Yandex OCR transport error attempt=%s/%s delay=%.1fs: %s",
                            attempt + 1,
                            self.max_retries,
                            delay,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(delay)

        if last_error is not None:
            raise RuntimeError(
                f"Yandex OCR retries exhausted: {type(last_error).__name__}"
            ) from last_error
        raise RuntimeError("Yandex OCR retries exhausted")


_OCR_REQUEST_LOCK = asyncio.Lock()
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_delay_seconds(attempt: int) -> float:
    # attempt 0 -> 1s, 1 -> 2s, 2 -> 4s
    return float(2 ** attempt)


REGISTRATION_OCR_KEYWORDS = {
    "ЗАРЕГИСТРИРОВАН": 5,
    "РЕГИСТРИРАЦИОННОГО": 3,
    "МЕСТО ЖИТЕЛЬСТВА": 4,
    "СНЯТ": 2,
    "УЛ.": 2,
    "ДОМ": 2,
    "КВ.": 2,
    "Г.": 1,
}

PASSPORT_MAIN_OCR_KEYWORDS = {
    "РОССИЙСКАЯ ФЕДЕРАЦИЯ": 5,
    "ПАСПОРТ": 5,
    "ПАСПОРТ ВЫДАН": 5,
    "ДАТА ВЫДАЧИ": 4,
    "КОД ПОДРАЗДЕЛЕНИЯ": 4,
    "ФАМИЛИЯ": 3,
    "ИМЯ": 3,
    "ОТЧЕСТВО": 3,
    "ДАТА РОЖДЕНИЯ": 3,
    "PNRUS": 2,
    "RUS": 1,
}

TIN_OCR_KEYWORDS = {
    "ИНН": 8,
    "НАЛОГ": 6,
    "НАЛОГОПЛАТЕЛЬЩИК": 6,
    "НАЛОГОВОМ": 5,
    "СВИДЕТЕЛЬСТВО": 5,
    "ПОСТАНОВКЕ НА УЧЕТ": 6,
    "ПО МЕСТУ ЖИТЕЛЬСТВА": 3,
    "РОССИЙСКАЯ ФЕДЕРАЦИЯ": 2,
    "МИНИСТЕРСТВО": 2,
    "ФЕДЕРАЛЬНОЙ НАЛОГОВОЙ СЛУЖБЫ": 6,
    "ИДЕНТИФИКАЦИОННЫЙ НОМЕР": 6,
    "НОМЕР НАЛОГОПЛАТЕЛЬЩИКА": 6,
}


def _prepare_rotated_image_bytes_candidates(
    file_content: bytes,
    extension: str,
    mime_type: str,
) -> list[tuple[int, bytes, str]]:
    image_bytes, _ = _prepare_image_bytes(file_content, extension, mime_type)
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    candidates: list[tuple[int, bytes, str]] = []
    for angle in (0, 90, 180, 270):
        rotated = image.rotate(angle, expand=True)
        candidates.append((angle, *_image_to_jpeg_bytes(rotated)))
    return candidates


def _score_ocr_text(text: str, score_profile: str) -> float:
    if score_profile == "registration":
        upper_text = text.upper()
        score = sum(
            weight for keyword, weight in REGISTRATION_OCR_KEYWORDS.items() if keyword in upper_text
        )
        score += min(len(text) / 100, 20)
        if len(text) < 30:
            score *= 0.2
        return score

    if score_profile == "passport_main":
        upper_text = text.upper().replace("Ё", "Е")
        score = sum(
            weight for keyword, weight in PASSPORT_MAIN_OCR_KEYWORDS.items() if keyword in upper_text
        )
        if _has_passport_side_number_pattern(text):
            score += 10
        score += min(len(text) / 100, 20)
        if len(text) < 30:
            score *= 0.2
        return score

    if score_profile == "tin":
        upper_text = text.upper().replace("Ё", "Е")
        score = sum(
            weight for keyword, weight in TIN_OCR_KEYWORDS.items() if keyword in upper_text
        )
        if _has_tin_number_pattern(text):
            score += 10
        score += min(len(text) / 100, 20)
        if len(text) < 30:
            score *= 0.2
        return score

    return float(len(text))


def _has_tin_number_pattern(text: str) -> bool:
    if re.search(r"(?<!\d)\d{12}(?!\d)", text):
        return True

    groups = re.findall(r"\d+", text)
    for start in range(len(groups)):
        combined = ""
        for group in groups[start:]:
            combined += group
            if len(combined) == 12:
                return True
            if len(combined) > 12:
                break
    return False


def _has_passport_side_number_pattern(text: str) -> bool:
    tokens: list[str] = []
    for line in text.splitlines():
        tokens.extend(re.findall(r"\d+", line))

    for index in range(len(tokens) - 2):
        if (
            re.fullmatch(r"\d{2}", tokens[index])
            and re.fullmatch(r"\d{2}", tokens[index + 1])
            and re.fullmatch(r"\d{6}", tokens[index + 2])
        ):
            return True
    return False


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
    if fitz is None:
        raise RuntimeError("PDF OCR requires PyMuPDF (fitz) dependency")
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
