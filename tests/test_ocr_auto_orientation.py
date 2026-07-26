from __future__ import annotations

import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.yandex_ocr_service import (
    YandexOCRService,
    _is_ocr_result_acceptable,
    _open_rgb_image_with_exif,
    _prepare_image_bytes,
    _score_ocr_text,
    score_profile_for_declared_document_type,
)


def _asymmetric_rgb_image() -> Image.Image:
    image = Image.new("RGB", (60, 30), (0, 0, 0))
    for x in range(30):
        for y in range(30):
            image.putpixel((x, y), (255, 0, 0))
            image.putpixel((x + 30, y), (0, 0, 255))
    return image


def _jpeg_with_exif_orientation(orientation: int) -> bytes:
    image = _asymmetric_rgb_image()
    exif = image.getexif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif)
    return buffer.getvalue()


def _plain_jpeg() -> bytes:
    buffer = io.BytesIO()
    _asymmetric_rgb_image().save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


GOOD_PASSPORT = (
    "РОССИЙСКАЯ ФЕДЕРАЦИЯ ПАСПОРТ ФАМИЛИЯ ИМЯ ОТЧЕСТВО "
    "ДАТА РОЖДЕНИЯ ДАТА ВЫДАЧИ КОД ПОДРАЗДЕЛЕНИЯ 80 11 541410"
)
GOOD_REGISTRATION = (
    "ЗАРЕГИСТРИРОВАН МЕСТО ЖИТЕЛЬСТВА ул. Ленина ДОМ 1 КВ. 2 "
    "регистрационного учета отделение района"
)
GOOD_TIN = (
    "ИНН СВИДЕТЕЛЬСТВО ПОСТАНОВКЕ НА УЧЕТ НАЛОГОПЛАТЕЛЬЩИК "
    "ФЕДЕРАЛЬНОЙ НАЛОГОВОЙ СЛУЖБЫ 523501879144"
)
GOOD_SNILS = (
    "СНИЛС СТРАХОВОЙ НОМЕР ИНДИВИДУАЛЬНОГО ЛИЦЕВОГО СЧЕТА "
    "ПЕНСИОННЫЙ ФОНД 123-456-789 00"
)
BAD_SHORT = "abc"


class ExifOrientationTests(unittest.TestCase):
    def test_exif_orientation_6_transposed(self) -> None:
        raw = _jpeg_with_exif_orientation(6)
        image = _open_rgb_image_with_exif(raw, "jpg", "image/jpeg")
        self.assertEqual(image.size, (30, 60))

    def test_exif_orientation_3_transposed(self) -> None:
        raw = _jpeg_with_exif_orientation(3)
        image = _open_rgb_image_with_exif(raw, "jpg", "image/jpeg")
        self.assertEqual(image.size, (60, 30))
        # Orientation 3 rotates 180°: former left-red becomes right side.
        # JPEG re-encode may shift exact RGB by 1; compare sides relatively.
        left = image.getpixel((15, 15))
        right = image.getpixel((45, 15))
        self.assertGreater(right[0], left[0])
        self.assertGreater(left[2], right[2])

    def test_prepare_image_bytes_does_not_mutate_original(self) -> None:
        original = _jpeg_with_exif_orientation(6)
        snapshot = bytes(original)
        prepared, mime = _prepare_image_bytes(original, "jpg", "image/jpeg")
        self.assertEqual(original, snapshot)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotEqual(prepared, original)


class ScoreProfileTests(unittest.TestCase):
    def test_declared_document_type_maps_to_profiles(self) -> None:
        self.assertEqual(
            score_profile_for_declared_document_type("passport_main"),
            "passport_main",
        )
        self.assertEqual(
            score_profile_for_declared_document_type("passport_registration"),
            "registration",
        )
        self.assertEqual(score_profile_for_declared_document_type("snils"), "snils")
        self.assertEqual(score_profile_for_declared_document_type("tin"), "tin")
        self.assertEqual(score_profile_for_declared_document_type(None), "generic")

    def test_snils_score_uses_keywords_and_pattern(self) -> None:
        score = _score_ocr_text(GOOD_SNILS, "snils")
        self.assertGreaterEqual(score, 6.0)
        self.assertTrue(_is_ocr_result_acceptable(GOOD_SNILS, score, "snils"))
        self.assertFalse(
            _is_ocr_result_acceptable(BAD_SHORT, _score_ocr_text(BAD_SHORT, "snils"), "snils")
        )


class AdaptiveOrientationTests(unittest.IsolatedAsyncioTestCase):
    async def test_good_angle_zero_makes_one_ocr_request(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize = AsyncMock(return_value=GOOD_PASSPORT)
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            text = await service.recognize_text_auto_oriented(
                _plain_jpeg(),
                "image/jpeg",
                filename="p.jpg",
                score_profile="passport_main",
            )
        self.assertEqual(text, GOOD_PASSPORT)
        self.assertEqual(recognize.await_count, 1)

    async def test_low_score_tries_additional_angles(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize = AsyncMock(
            side_effect=[BAD_SHORT, BAD_SHORT, BAD_SHORT, BAD_SHORT]
        )
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            await service.recognize_text_auto_oriented(
                _plain_jpeg(),
                "image/jpeg",
                filename="p.jpg",
                score_profile="passport_main",
            )
        self.assertEqual(recognize.await_count, 4)

    async def test_best_result_on_90_degrees(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize = AsyncMock(
            side_effect=[BAD_SHORT, GOOD_PASSPORT, BAD_SHORT, BAD_SHORT]
        )
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            text = await service.recognize_text_auto_oriented(
                _plain_jpeg(),
                "image/jpeg",
                filename="p.jpg",
                score_profile="passport_main",
            )
        self.assertEqual(text, GOOD_PASSPORT)
        self.assertEqual(recognize.await_count, 4)

    async def test_best_result_on_180_degrees(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize = AsyncMock(
            side_effect=[BAD_SHORT, BAD_SHORT, GOOD_REGISTRATION, BAD_SHORT]
        )
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            text = await service.recognize_text_auto_oriented(
                _plain_jpeg(),
                "image/jpeg",
                filename="r.jpg",
                score_profile="registration",
            )
        self.assertEqual(text, GOOD_REGISTRATION)

    async def test_best_result_on_270_degrees(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize = AsyncMock(
            side_effect=[BAD_SHORT, BAD_SHORT, BAD_SHORT, GOOD_TIN]
        )
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            text = await service.recognize_text_auto_oriented(
                _plain_jpeg(),
                "image/jpeg",
                filename="t.jpg",
                score_profile="tin",
            )
        self.assertEqual(text, GOOD_TIN)

    async def test_pdf_skips_rotation_search(self) -> None:
        service = YandexOCRService(api_key="test")
        recognize_text = AsyncMock(return_value="pdf text")
        with patch.object(YandexOCRService, "recognize_text", recognize_text):
            text = await service.recognize_text_auto_oriented(
                b"%PDF-1.4 fake",
                "application/pdf",
                filename="doc.pdf",
                score_profile="passport_main",
            )
        self.assertEqual(text, "pdf text")
        recognize_text.assert_awaited_once()

    async def test_original_bytes_unchanged_after_auto_orient(self) -> None:
        service = YandexOCRService(api_key="test")
        original = _jpeg_with_exif_orientation(6)
        snapshot = bytes(original)
        recognize = AsyncMock(return_value=GOOD_PASSPORT)
        with patch.object(YandexOCRService, "_recognize_image", recognize):
            await service.recognize_text_auto_oriented(
                original,
                "image/jpeg",
                filename="p.jpg",
                score_profile="passport_main",
            )
        self.assertEqual(original, snapshot)


class DeclaredTypeAndGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_for_each_declared_type(self) -> None:
        cases = (
            ("passport_main", "passport_main"),
            ("passport_registration", "registration"),
            ("snils", "snils"),
            ("tin", "tin"),
        )
        for declared, profile in cases:
            ocr = AsyncMock()
            ocr.recognize_text_auto_oriented = AsyncMock(return_value=GOOD_PASSPORT)
            gpt = AsyncMock()
            gpt.complete = AsyncMock(
                return_value=(
                    '{"document_type":"unknown","confidence":0.1,'
                    '"reason":null,"detected_documents":[]}'
                )
            )
            service = CustomerDocumentRecognitionService(
                ocr_service=ocr,
                gpt_service=gpt,
            )
            await service.recognize_document(
                b"img",
                "image/jpeg",
                "a.jpg",
                declared_document_type=declared,
            )
            self.assertEqual(
                ocr.recognize_text_auto_oriented.await_args.kwargs["score_profile"],
                profile,
            )

    async def test_declared_type_does_not_bypass_guard(self) -> None:
        ocr = AsyncMock()
        ocr.recognize_text_auto_oriented = AsyncMock(return_value=GOOD_TIN)
        gpt = AsyncMock()
        gpt.complete = AsyncMock(
            side_effect=[
                '{"document_type":"tin","confidence":0.9,"reason":null,"detected_documents":[]}',
                '{"document_type":"tin","tin":"523501879144","last_name":"Ivanov",'
                '"first_name":"Ivan","surname":null,"confidence":{},"warnings":[]}',
            ]
        )
        service = CustomerDocumentRecognitionService(ocr_service=ocr, gpt_service=gpt)
        result = await service.recognize_document(
            b"img",
            "image/jpeg",
            "a.jpg",
            declared_document_type="snils",
        )
        self.assertEqual(result.document_type, "tin")
        self.assertTrue(
            any("declared_document_type=snils" in item for item in result.warnings)
        )

    async def test_add_customer_passport_main_uses_auto_orient(self) -> None:
        ocr = AsyncMock()
        ocr.recognize_text_auto_oriented = AsyncMock(return_value=GOOD_PASSPORT)
        gpt = AsyncMock()
        gpt.complete = AsyncMock(
            side_effect=[
                '{"document_type":"passport_main","confidence":0.9,"reason":null,"detected_documents":[]}',
                '{"document_type":"passport_main","passport":"80 11 541410","last_name":"Ivanov",'
                '"first_name":"Ivan","surname":null,"by_whom_issued":"МВД","date_issue":"01.01.2020",'
                '"department_code":"020-001","confidence":{},"warnings":[]}',
            ]
        )
        service = CustomerDocumentRecognitionService(ocr_service=ocr, gpt_service=gpt)
        result = await service.recognize_passport_main(
            b"img",
            "image/jpeg",
            filename="passport.jpg",
        )
        self.assertNotIn("document_type_mismatch", result)
        self.assertEqual(
            ocr.recognize_text_auto_oriented.await_args.kwargs["score_profile"],
            "passport_main",
        )

    async def test_add_customer_snils_uses_snils_profile(self) -> None:
        ocr = AsyncMock()
        ocr.recognize_text_auto_oriented = AsyncMock(return_value=GOOD_SNILS)
        gpt = AsyncMock()
        gpt.complete = AsyncMock(
            side_effect=[
                '{"document_type":"snils","confidence":0.9,"reason":null,"detected_documents":[]}',
                '{"document_type":"snils","ipain":"123-456-789 00","last_name":"Ivanov",'
                '"first_name":"Ivan","surname":null,"confidence":{},"warnings":[]}',
            ]
        )
        service = CustomerDocumentRecognitionService(ocr_service=ocr, gpt_service=gpt)
        result = await service.recognize_snils(b"img", "image/jpeg", filename="snils.jpg")
        self.assertEqual(result.get("ipain"), "123-456-789 00")
        self.assertEqual(
            ocr.recognize_text_auto_oriented.await_args.kwargs["score_profile"],
            "snils",
        )


class HeicRegressionTests(unittest.TestCase):
    def test_heic_prepare_requires_pillow_heif_or_works(self) -> None:
        try:
            from pillow_heif import register_heif_opener  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError):
                _prepare_image_bytes(b"not-heic", "heic", "image/heic")
            return
        # With pillow-heif installed, invalid payload still fails at Image.open.
        with self.assertRaises(Exception):
            _prepare_image_bytes(b"not-heic", "heic", "image/heic")


class MiniAppHintTests(unittest.TestCase):
    def test_upload_hint_mentions_any_orientation(self) -> None:
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "app/web/static/miniapp.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Можно загружать фото в любой ориентации",
            js,
        )


class BatchDeclaredTypePassThroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_recognize_passes_declared_document_type(self) -> None:
        from app.services.customer_batch_recognition_service import (
            CustomerBatchRecognitionService,
        )
        from app.services.customer_document_recognition_service import (
            CustomerDocumentRecognitionResult,
        )

        repository = AsyncMock()
        repository.mark_file_processing = AsyncMock()
        repository.update_file_recognition = AsyncMock()
        recognition = AsyncMock()
        recognition.recognize_document = AsyncMock(
            return_value=CustomerDocumentRecognitionResult(
                document_type="tin",
                confidence=0.9,
                ocr_text=GOOD_TIN,
                extracted_fields={"tin": "523501879144"},
                warnings=[],
            )
        )
        service = CustomerBatchRecognitionService(
            repository=repository,
            recognition_service=recognition,
        )
        await service._recognize_one_file(
            batch_id=1,
            file_row={
                "id": 10,
                "temporary_content": b"img",
                "mime_type": "image/jpeg",
                "original_filename": "tin.jpg",
                "declared_document_type": "tin",
            },
        )
        recognition.recognize_document.assert_awaited_once()
        self.assertEqual(
            recognition.recognize_document.await_args.kwargs["declared_document_type"],
            "tin",
        )


if __name__ == "__main__":
    unittest.main()
