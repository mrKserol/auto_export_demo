from __future__ import annotations

import base64
import json
import unittest
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.services.miniapp_link_service import create_customer_estimate_token
from app.web.app import create_fastapi_app
from app.web.token_service import (
    TokenError,
    create_estimate_context_token,
    create_specification_context_token,
    verify_estimate_context_token,
)


BOT_TOKEN = "123456:ABC-DEF"
SECRET = "mini-app-secret"
NOW = 1_700_000_000


@dataclass(frozen=True)
class DummySettings:
    telegram_bot_token: str = BOT_TOKEN
    mini_app_token_secret: str = SECRET
    telegram_init_data_max_age_seconds: int = 900
    mini_app_base_url: str = "https://app.example.com"
    mini_app_token_ttl_seconds: int = 900
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    database_url: str = "postgresql://example"
    yandex_disk_token: str = "x"
    yandex_disk_base_path: str = "/x"
    yandex_function_url: str | None = None
    enable_processing: bool = False
    yandex_api_key: str = "x"
    yandex_cloud_folder_id: str = "x"
    ocr_min_delay_seconds: float = 1.5
    max_ocr_retries: int = 5


def _decode_token_payload(token: str) -> dict:
    payload_part, _signature_part = token.rsplit(".", 1)
    padding = "=" * (-len(payload_part) % 4)
    raw = base64.urlsafe_b64decode(payload_part + padding).decode("utf-8")
    return json.loads(raw)


class EstimateMiniAppTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests only verify static HTML and token logic.
        # We don't need a real database or Telegram bot for /miniapp/estimate.
        self.database = object()
        self.bot = object()
        self.app = create_fastapi_app(
            settings=DummySettings(),
            database=self.database,  # type: ignore[arg-type]
            bot=self.bot,  # type: ignore[arg-type]
        )
        self.client = TestClient(self.app)

    def test_estimate_page_content(self) -> None:
        response = self.client.get("/miniapp/estimate?token=abc")
        self.assertEqual(response.status_code, 200)

        self.assertIn("Расчёт сметы", response.text)
        self.assertIn("/static/estimate.js", response.text)
        self.assertIn("/static/estimate.css", response.text)

        self.assertIn("/static/estimate.js?v=2", response.text)
        self.assertIn("/static/estimate.css?v=2", response.text)

        self.assertNotIn("/static/specification.js", response.text)
        self.assertNotIn("Спецификация автомобиля", response.text)

    def test_create_customer_estimate_token_purpose(self) -> None:
        settings = DummySettings()
        token = create_customer_estimate_token(
            settings,
            customer_id=10,
            telegram_user_id=456,
            origin_chat_id=456,
        )
        payload = _decode_token_payload(token)
        self.assertEqual(payload["purpose"], "create_estimate")

    def test_estimate_token_verification_accepts_estimate(self) -> None:
        settings = DummySettings()
        token = create_estimate_context_token(
            secret=settings.mini_app_token_secret,
            customer_id=10,
            telegram_user_id=456,
            origin_chat_id=456,
            ttl_seconds=900,
            now=NOW,
        )
        ctx = verify_estimate_context_token(
            token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=456,
            now=NOW,
        )
        self.assertEqual(ctx.purpose, "create_estimate")

    def test_estimate_token_verification_rejects_specification(self) -> None:
        settings = DummySettings()
        token = create_specification_context_token(
            secret=settings.mini_app_token_secret,
            customer_id=10,
            telegram_user_id=456,
            origin_chat_id=456,
            ttl_seconds=900,
            now=NOW,
        )
        with self.assertRaises(TokenError) as ctx:
            verify_estimate_context_token(
                token,
                secret=settings.mini_app_token_secret,
                expected_telegram_user_id=456,
                now=NOW,
            )
        self.assertEqual(ctx.exception.code, "INVALID_CONTEXT_TOKEN")


if __name__ == "__main__":
    unittest.main()

