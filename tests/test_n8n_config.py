from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import load_settings


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


class LoadSettingsN8nConfigTests(unittest.TestCase):
    def test_missing_n8n_settings_are_none(self) -> None:
        with patch.dict(os.environ, _required_env(), clear=True):
            settings = load_settings()
        self.assertIsNone(settings.n8n_telegram_webhook_url)
        self.assertIsNone(settings.n8n_webhook_secret)

    def test_both_n8n_settings_are_loaded(self) -> None:
        env = {
            **_required_env(),
            "N8N_TELEGRAM_WEBHOOK_URL": "  https://n8n.example/webhook/telegram  ",
            "N8N_WEBHOOK_SECRET": "  secret-value  ",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(
            settings.n8n_telegram_webhook_url,
            "https://n8n.example/webhook/telegram",
        )
        self.assertEqual(settings.n8n_webhook_secret, "secret-value")

    def test_only_webhook_url_raises(self) -> None:
        env = {
            **_required_env(),
            "N8N_TELEGRAM_WEBHOOK_URL": "https://n8n.example/webhook/telegram",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "N8N_TELEGRAM_WEBHOOK_URL and N8N_WEBHOOK_SECRET "
                "must be configured together",
            ):
                load_settings()

    def test_only_webhook_secret_raises(self) -> None:
        env = {
            **_required_env(),
            "N8N_WEBHOOK_SECRET": "secret-value",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "N8N_TELEGRAM_WEBHOOK_URL and N8N_WEBHOOK_SECRET "
                "must be configured together",
            ):
                load_settings()


if __name__ == "__main__":
    unittest.main()
