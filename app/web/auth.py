from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool | None = None
    raw: dict | None = None


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int,
    *,
    now: int | None = None,
) -> TelegramUser:
    if not init_data or not init_data.strip():
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Telegram initData отсутствует",
        )

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except ValueError as error:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Telegram initData повреждён",
        ) from error

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "В Telegram initData отсутствует hash",
        )

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(data.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Подпись Telegram initData недействительна",
        )

    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "В Telegram initData отсутствует auth_date",
        )
    try:
        auth_date = int(auth_date_raw)
    except ValueError as error:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Некорректный auth_date в Telegram initData",
        ) from error

    current_time = int(time.time() if now is None else now)
    if auth_date > current_time + 60:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Некорректный auth_date в Telegram initData",
        )
    if current_time - auth_date > max_age_seconds:
        raise InitDataError(
            "EXPIRED_TELEGRAM_INIT_DATA",
            "Срок действия Telegram initData истёк",
        )

    user_raw = data.get("user")
    if not user_raw:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "В Telegram initData отсутствует пользователь",
        )

    try:
        user_payload = json.loads(user_raw)
    except json.JSONDecodeError as error:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "JSON пользователя в Telegram initData повреждён",
        ) from error

    if not isinstance(user_payload, dict) or "id" not in user_payload:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "В Telegram initData отсутствует пользователь",
        )

    try:
        user_id = int(user_payload["id"])
    except (TypeError, ValueError) as error:
        raise InitDataError(
            "INVALID_TELEGRAM_INIT_DATA",
            "Некорректный ID пользователя в Telegram initData",
        ) from error

    return TelegramUser(
        id=user_id,
        first_name=user_payload.get("first_name"),
        last_name=user_payload.get("last_name"),
        username=user_payload.get("username"),
        language_code=user_payload.get("language_code"),
        is_premium=user_payload.get("is_premium"),
        raw=user_payload,
    )


def build_telegram_init_data_for_tests(
    *,
    bot_token: str,
    user: dict,
    auth_date: int,
    extra: dict[str, str] | None = None,
) -> str:
    """Helper for unit tests: builds a correctly signed initData string."""
    from urllib.parse import urlencode

    payload = {
        "auth_date": str(auth_date),
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    if extra:
        payload.update(extra)

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(payload.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload["hash"] = calculated_hash
    return urlencode(payload)
