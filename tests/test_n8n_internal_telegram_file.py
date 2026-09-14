from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.web.app import create_fastapi_app
from tests.test_telegram_proxy import _settings as _base_settings


WEBHOOK_SECRET = "n8n-test-secret-value"


def _settings(**overrides):
    values = {
        "n8n_telegram_webhook_url": "https://n8n.example/webhook/telegram",
        "n8n_webhook_secret": WEBHOOK_SECRET,
    }
    values.update(overrides)
    return _base_settings(**values)


class InternalTelegramFileEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = AsyncMock()
        self.app = create_fastapi_app(
            settings=_settings(),
            database=AsyncMock(),
            bot=self.bot,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_downloads_telegram_file_and_returns_binary_response(self) -> None:
        self.bot.get_file.return_value = SimpleNamespace(
            file_path="documents/passport.pdf"
        )

        async def download_file(file_path, *, destination, **kwargs):
            self.assertEqual(file_path, "documents/passport.pdf")
            self.assertIsInstance(destination, BytesIO)
            destination.write(b"%PDF-test")
            return destination

        self.bot.download_file.side_effect = download_file

        response = await self.client.post(
            "/internal/n8n/telegram-file",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            json={"file_id": "telegram-file-id"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-test")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn(
            'attachment; filename="passport.pdf"',
            response.headers["content-disposition"],
        )
        self.bot.get_file.assert_awaited_once_with("telegram-file-id")
        self.bot.download_file.assert_awaited_once()

    async def test_missing_file_id_returns_422(self) -> None:
        response = await self.client.post(
            "/internal/n8n/telegram-file",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            json={},
        )

        self.assertEqual(response.status_code, 422)
        self.bot.get_file.assert_not_awaited()
        self.bot.download_file.assert_not_awaited()

    async def test_wrong_secret_returns_401(self) -> None:
        response = await self.client.post(
            "/internal/n8n/telegram-file",
            headers={"X-N8N-Webhook-Secret": "wrong-secret"},
            json={"file_id": "telegram-file-id"},
        )

        self.assertEqual(response.status_code, 401)
        self.bot.get_file.assert_not_awaited()
        self.bot.download_file.assert_not_awaited()

    async def test_telegram_error_returns_502(self) -> None:
        self.bot.get_file.side_effect = RuntimeError("telegram is unavailable")

        response = await self.client.post(
            "/internal/n8n/telegram-file",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            json={"file_id": "telegram-file-id"},
        )

        self.assertEqual(response.status_code, 502)
        self.bot.get_file.assert_awaited_once_with("telegram-file-id")
        self.bot.download_file.assert_not_awaited()


class InternalCustomerDocumentRecognitionEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.recognition_service = AsyncMock()
        self.app = create_fastapi_app(
            settings=_settings(),
            database=AsyncMock(),
            bot=AsyncMock(),
            customer_document_recognition_service=self.recognition_service,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_recognizes_customer_document(self) -> None:
        self.recognition_service.recognize_document.return_value = SimpleNamespace(
            document_type="passport_main",
            confidence=0.91,
            extracted_fields={
                "last_name": "Иванов",
                "first_name": "Иван",
                "surname": "Иванович",
                "passport": "80 06 035956",
                "date_issue": "01.02.2020",
                "department_code": "123-456",
                "birth_date": "03.04.1990",
                "birth_place": "г. Уфа",
                "warnings": ["low_contrast"],
            },
            warnings=["guard_warning"],
        )

        response = await self.client.post(
            "/internal/n8n/recognize-customer-document",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            data={
                "original_name": "passport.jpg",
                "mime_type": "image/jpeg",
            },
            files={"file": ("passport.jpg", b"image-bytes", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        self.recognition_service.recognize_document.assert_awaited_once_with(
            content=b"image-bytes",
            mime_type="image/jpeg",
            filename="passport.jpg",
        )
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "document": {
                    "surname": "Иванов",
                    "first_name": "Иван",
                    "patronymic": "Иванович",
                    "passport": "80 06 035956",
                    "date_issue": "01.02.2020",
                    "department_code": "123-456",
                    "birth_date": "03.04.1990",
                    "birth_place": "г. Уфа",
                    "registration_address": None,
                    "snils": None,
                    "tin": None,
                    "document_type": "passport_main",
                    "confidence": 0.91,
                    "warnings": ["guard_warning", "low_contrast"],
                },
            },
        )

    async def test_recognize_missing_file_returns_422(self) -> None:
        response = await self.client.post(
            "/internal/n8n/recognize-customer-document",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            data={"original_name": "passport.jpg"},
        )

        self.assertEqual(response.status_code, 422)
        self.recognition_service.recognize_document.assert_not_awaited()

    async def test_recognize_wrong_secret_returns_401(self) -> None:
        response = await self.client.post(
            "/internal/n8n/recognize-customer-document",
            headers={"X-N8N-Webhook-Secret": "wrong-secret"},
            files={"file": ("passport.jpg", b"image-bytes", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 401)
        self.recognition_service.recognize_document.assert_not_awaited()

    async def test_recognize_telegram_pipeline_error_returns_502(self) -> None:
        self.recognition_service.recognize_document.side_effect = RuntimeError(
            "OCR failed"
        )

        response = await self.client.post(
            "/internal/n8n/recognize-customer-document",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            files={"file": ("passport.jpg", b"image-bytes", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 502)
        self.recognition_service.recognize_document.assert_awaited_once()

    async def test_recognize_removes_temporary_file(self) -> None:
        self.recognition_service.recognize_document.return_value = SimpleNamespace(
            document_type="snils",
            confidence=0.8,
            extracted_fields={"ipain": "123-456-789 00"},
            warnings=[],
        )
        original_named_temporary_file = tempfile.NamedTemporaryFile

        with tempfile.TemporaryDirectory() as temp_dir:
            created_paths: list[Path] = []

            def named_temporary_file(*args, **kwargs):
                kwargs["dir"] = temp_dir
                temporary = original_named_temporary_file(*args, **kwargs)
                created_paths.append(Path(temporary.name))
                return temporary

            with patch(
                "app.web.routes.tempfile.NamedTemporaryFile",
                side_effect=named_temporary_file,
            ):
                response = await self.client.post(
                    "/internal/n8n/recognize-customer-document",
                    headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
                    files={"file": ("snils.jpg", b"image-bytes", "image/jpeg")},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(created_paths)
            self.assertTrue(all(not path.exists() for path in created_paths))


class InternalDocumentIntakeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.intake_service = AsyncMock()
        self.app = create_fastapi_app(
            settings=_settings(),
            database=AsyncMock(),
            bot=AsyncMock(),
            document_intake_service=self.intake_service,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_intake_session_requires_secret(self) -> None:
        response = await self.client.post(
            "/internal/n8n/intake-sessions",
            json={
                "channel": "telegram",
                "external_user_id": "316257868",
                "conversation_id": "316257868",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 401)
        self.intake_service.create_or_get_session.assert_not_awaited()

    async def test_creates_intake_session(self) -> None:
        self.intake_service.create_or_get_session.return_value = {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "status": "collecting",
            "expires_at": "2026-09-14T10:00:00+00:00",
        }

        response = await self.client.post(
            "/internal/n8n/intake-sessions",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
            json={
                "channel": "telegram",
                "external_user_id": "316257868",
                "conversation_id": "316257868",
                "metadata": {"source": "n8n"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "collecting")
        self.intake_service.create_or_get_session.assert_awaited_once_with(
            channel="telegram",
            external_user_id="316257868",
            conversation_id="316257868",
            metadata={"source": "n8n"},
        )

    async def test_uploads_intake_document_and_removes_temporary_file(self) -> None:
        self.intake_service.add_document.return_value = {
            "ok": True,
            "session_id": "11111111-1111-1111-1111-111111111111",
            "document_id": "22222222-2222-2222-2222-222222222222",
            "duplicate": False,
            "status": "recognized",
            "document": {"document_type": "tin"},
        }
        original_named_temporary_file = tempfile.NamedTemporaryFile

        with tempfile.TemporaryDirectory() as temp_dir:
            created_paths: list[Path] = []

            def named_temporary_file(*args, **kwargs):
                kwargs["dir"] = temp_dir
                temporary = original_named_temporary_file(*args, **kwargs)
                created_paths.append(Path(temporary.name))
                return temporary

            with patch(
                "app.web.routes.tempfile.NamedTemporaryFile",
                side_effect=named_temporary_file,
            ):
                response = await self.client.post(
                    "/internal/n8n/intake-sessions/"
                    "11111111-1111-1111-1111-111111111111/documents",
                    headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
                    data={
                        "channel": "telegram",
                        "external_user_id": "316257868",
                        "conversation_id": "316257868",
                        "provider_message_id": "790",
                        "provider_file_id": "file-id",
                        "media_group_id": "album-1",
                        "original_name": "tin.jpg",
                        "mime_type": "image/jpeg",
                    },
                    files={"file": ("tin.jpg", b"image-bytes", "image/jpeg")},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(created_paths)
            self.assertTrue(all(not path.exists() for path in created_paths))

        call = self.intake_service.add_document.await_args
        self.assertIsNotNone(call)
        upload = call.args[1]
        self.assertEqual(upload.content, b"image-bytes")
        self.assertEqual(upload.provider_file_id, "file-id")
        self.assertEqual(upload.media_group_id, "album-1")

    async def test_finish_intake_session(self) -> None:
        self.intake_service.finish.return_value = {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "status": "review",
            "ready_for_confirmation": False,
            "reasons": ["missing_document_types"],
        }

        response = await self.client.post(
            "/internal/n8n/intake-sessions/"
            "11111111-1111-1111-1111-111111111111/finish",
            headers={"X-N8N-Webhook-Secret": WEBHOOK_SECRET},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "review")
        self.intake_service.finish.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
