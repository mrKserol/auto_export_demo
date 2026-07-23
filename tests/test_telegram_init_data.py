from __future__ import annotations

import unittest
from urllib.parse import parse_qsl, urlencode

from app.web.auth import (
    InitDataError,
    build_telegram_init_data_for_tests,
    validate_telegram_init_data,
)


BOT_TOKEN = "123456:ABC-DEF"
NOW = 1_700_000_000
USER = {"id": 456, "first_name": "Ivan", "username": "ivan"}


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_init_data(self) -> None:
        init_data = build_telegram_init_data_for_tests(
            bot_token=BOT_TOKEN,
            user=USER,
            auth_date=NOW - 10,
        )
        user = validate_telegram_init_data(
            init_data,
            BOT_TOKEN,
            max_age_seconds=900,
            now=NOW,
        )
        self.assertEqual(user.id, 456)
        self.assertEqual(user.first_name, "Ivan")
        self.assertEqual(user.username, "ivan")

    def test_invalid_hash(self) -> None:
        init_data = build_telegram_init_data_for_tests(
            bot_token=BOT_TOKEN,
            user=USER,
            auth_date=NOW - 10,
        )
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        pairs["hash"] = "0" * 64
        with self.assertRaises(InitDataError) as raised:
            validate_telegram_init_data(
                urlencode(pairs),
                BOT_TOKEN,
                max_age_seconds=900,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "INVALID_TELEGRAM_INIT_DATA")

    def test_missing_hash(self) -> None:
        init_data = build_telegram_init_data_for_tests(
            bot_token=BOT_TOKEN,
            user=USER,
            auth_date=NOW - 10,
        )
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        pairs.pop("hash")
        with self.assertRaises(InitDataError) as raised:
            validate_telegram_init_data(
                urlencode(pairs),
                BOT_TOKEN,
                max_age_seconds=900,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "INVALID_TELEGRAM_INIT_DATA")

    def test_expired_auth_date(self) -> None:
        init_data = build_telegram_init_data_for_tests(
            bot_token=BOT_TOKEN,
            user=USER,
            auth_date=NOW - 1000,
        )
        with self.assertRaises(InitDataError) as raised:
            validate_telegram_init_data(
                init_data,
                BOT_TOKEN,
                max_age_seconds=900,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "EXPIRED_TELEGRAM_INIT_DATA")

    def test_missing_user(self) -> None:
        import hashlib
        import hmac
        from urllib.parse import urlencode

        payload = {"auth_date": str(NOW - 10)}
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(payload.items())
        )
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        payload["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        with self.assertRaises(InitDataError) as raised:
            validate_telegram_init_data(
                urlencode(payload),
                BOT_TOKEN,
                max_age_seconds=900,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "INVALID_TELEGRAM_INIT_DATA")
        self.assertIn("пользователь", raised.exception.message)

    def test_corrupted_user_json(self) -> None:
        import hashlib
        import hmac
        from urllib.parse import urlencode

        payload = {
            "auth_date": str(NOW - 10),
            "user": "{not-json",
        }
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(payload.items())
        )
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        payload["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        with self.assertRaises(InitDataError) as raised:
            validate_telegram_init_data(
                urlencode(payload),
                BOT_TOKEN,
                max_age_seconds=900,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "INVALID_TELEGRAM_INIT_DATA")
        self.assertIn("повреждён", raised.exception.message)

if __name__ == "__main__":
    unittest.main()
