from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.web.app import create_fastapi_app
from app.web.auth import build_telegram_init_data_for_tests
from app.web.token_service import create_specification_context_token


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


def _valid_body(**overrides):
    token = create_specification_context_token(
        secret=SECRET,
        customer_id=10,
        telegram_user_id=456,
        origin_chat_id=456,
        ttl_seconds=900,
        now=NOW,
    )
    init_data = build_telegram_init_data_for_tests(
        bot_token=BOT_TOKEN,
        user={"id": 456, "first_name": "Ivan"},
        auth_date=NOW - 5,
    )
    payload = {
        "brand": "Buick",
        "model": "Encore",
        "year": 2023,
        "eng_capacity": "1.3",
        "eng_type": "Бензин",
        "drive": "Полный",
        "transmission": "АКПП",
        "color": "белый",
        "complectation": "RS",
        "mileage": 50000,
        "price": "100000",
        "currency": "CNY",
        "context_token": token,
        "telegram_init_data": init_data,
    }
    payload.update(overrides)
    return payload


class SpecificationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = AsyncMock()
        self.bot = AsyncMock()
        self.app = create_fastapi_app(
            settings=DummySettings(),
            database=self.database,
            bot=self.bot,
        )
        self.client = TestClient(self.app)

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_form_page(self) -> None:
        response = self.client.get("/miniapp/specification?token=abc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Спецификация автомобиля", response.text)

    @patch("app.web.routes.create_customer_specification", new_callable=AsyncMock)
    @patch("app.web.routes._notify_user_about_specification", new_callable=AsyncMock)
    def test_successful_create(self, notify_mock, create_mock) -> None:
        create_mock.return_value = {
            "specification_id": 77,
            "customer": {"id": 10},
            "specification": {"brand": "Buick"},
        }
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            response = self.client.post("/api/specifications", json=_valid_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["specification_id"], 77)
        create_mock.assert_awaited()
        notify_mock.assert_awaited()

    def test_invalid_init_data(self) -> None:
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            payload = _valid_body(telegram_init_data="auth_date=1&user=%7B%22id%22%3A1%7D&hash=" + ("0" * 64))
            response = self.client.post("/api/specifications", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_TELEGRAM_INIT_DATA")

    def test_invalid_context_token(self) -> None:
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            payload = _valid_body(context_token="broken.token")
            response = self.client.post("/api/specifications", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_CONTEXT_TOKEN")

    def test_user_mismatch(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=10,
            telegram_user_id=999,
            origin_chat_id=999,
            ttl_seconds=900,
            now=NOW,
        )
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            response = self.client.post(
                "/api/specifications",
                json=_valid_body(context_token=token),
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "USER_MISMATCH")

    @patch("app.web.routes.create_customer_specification", new_callable=AsyncMock)
    def test_customer_not_found(self, create_mock) -> None:
        from app.services.specification_service import CustomerNotFoundError

        create_mock.side_effect = CustomerNotFoundError(10)
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            response = self.client.post("/api/specifications", json=_valid_body())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "CUSTOMER_NOT_FOUND")

    @patch("app.web.routes.create_customer_specification", new_callable=AsyncMock)
    def test_specification_already_exists(self, create_mock) -> None:
        from app.services.specification_service import SpecificationAlreadyExistsError

        create_mock.side_effect = SpecificationAlreadyExistsError(10, 55)
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            response = self.client.post("/api/specifications", json=_valid_body())
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"]["code"], "SPECIFICATION_ALREADY_EXISTS")
        self.assertEqual(body["error"]["specification_id"], 55)

    def test_validation_error(self) -> None:
        with patch("app.web.auth.time.time", return_value=NOW), patch(
            "app.web.token_service.time.time", return_value=NOW
        ):
            response = self.client.post(
                "/api/specifications",
                json=_valid_body(brand="", price="0"),
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

if __name__ == "__main__":
    unittest.main()
