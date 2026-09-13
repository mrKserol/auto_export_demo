from __future__ import annotations

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


if __name__ == "__main__":
    unittest.main()
