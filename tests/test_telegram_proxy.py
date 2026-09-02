from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import _stop_polling_if_started, create_bot
from app.config import Settings, load_settings


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "123456:ABC-DEF",
        "database_url": "postgresql://example",
        "yandex_disk_token": "token",
        "yandex_disk_base_path": "/auto_export_demo",
        "yandex_function_url": None,
        "enable_processing": False,
        "yandex_api_key": "key",
        "yandex_cloud_folder_id": "folder",
        "ocr_min_delay_seconds": 1.5,
        "max_ocr_retries": 3,
        "mini_app_base_url": "https://example.com",
        "mini_app_token_secret": "secret",
        "web_host": "0.0.0.0",
        "web_port": 8000,
        "mini_app_token_ttl_seconds": 900,
        "telegram_init_data_max_age_seconds": 900,
    }
    values.update(overrides)
    return Settings(**values)


def _required_env() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
        "DATABASE_URL": "postgresql://example",
        "YANDEX_DISK_TOKEN": "token",
        "YANDEX_API_KEY": "key",
        "YANDEX_CLOUD_FOLDER_ID": "folder",
        "MINI_APP_BASE_URL": "https://example.com",
        "MINI_APP_TOKEN_SECRET": "mini-app-secret",
    }


class LoadSettingsTelegramProxyTests(unittest.TestCase):
    def test_missing_telegram_proxy_url_is_none(self) -> None:
        env = _required_env()
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertIsNone(settings.telegram_proxy_url)

    def test_empty_telegram_proxy_url_is_none(self) -> None:
        env = {**_required_env(), "TELEGRAM_PROXY_URL": "   "}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertIsNone(settings.telegram_proxy_url)

    def test_telegram_proxy_url_is_stripped(self) -> None:
        env = {
            **_required_env(),
            "TELEGRAM_PROXY_URL": "  http://201.51.20.96:3128  ",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.telegram_proxy_url, "http://201.51.20.96:3128")


class CreateBotTelegramProxyTests(unittest.TestCase):
    @patch("app.bot.Bot")
    def test_create_bot_without_proxy_uses_default_session(self, bot_cls: MagicMock) -> None:
        settings = _settings(telegram_proxy_url=None)
        create_bot(settings)
        bot_cls.assert_called_once_with(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    @patch("app.bot.Bot")
    @patch("app.bot.AiohttpSession")
    def test_create_bot_with_proxy_uses_proxy_session(
        self,
        session_cls: MagicMock,
        bot_cls: MagicMock,
    ) -> None:
        proxy_url = "http://201.51.20.96:3128"
        session = MagicMock()
        session_cls.return_value = session
        settings = _settings(telegram_proxy_url=proxy_url)
        create_bot(settings)
        session_cls.assert_called_once_with(proxy=proxy_url)
        bot_cls.assert_called_once_with(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=session,
        )


class StopPollingGracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_polling_ignores_not_started_runtime_error(self) -> None:
        dispatcher = AsyncMock()
        dispatcher.stop_polling.side_effect = RuntimeError("Polling is not started")
        await _stop_polling_if_started(dispatcher)
        dispatcher.stop_polling.assert_awaited_once()

    async def test_stop_polling_reraises_other_runtime_errors(self) -> None:
        dispatcher = AsyncMock()
        dispatcher.stop_polling.side_effect = RuntimeError("unexpected failure")
        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            await _stop_polling_if_started(dispatcher)


if __name__ == "__main__":
    unittest.main()
