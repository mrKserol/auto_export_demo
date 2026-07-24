from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any


PURPOSE_CREATE_SPECIFICATION = "create_specification"
PURPOSE_CREATE_ESTIMATE = "create_estimate"
PURPOSE_EDIT_SPECIFICATION = "edit_specification"
PURPOSE_EDIT_CUSTOMER = "edit_customer"


class TokenError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class SpecificationContextToken:
    purpose: str
    customer_id: int
    telegram_user_id: int
    origin_chat_id: int
    exp: int
    nonce: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "customer_id": self.customer_id,
            "telegram_user_id": self.telegram_user_id,
            "origin_chat_id": self.origin_chat_id,
            "exp": self.exp,
            "nonce": self.nonce,
        }


def create_specification_context_token(
    *,
    secret: str,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    current_time = int(time.time() if now is None else now)
    payload = SpecificationContextToken(
        purpose=PURPOSE_CREATE_SPECIFICATION,
        customer_id=int(customer_id),
        telegram_user_id=int(telegram_user_id),
        origin_chat_id=int(origin_chat_id),
        exp=current_time + int(ttl_seconds),
        nonce=secrets.token_urlsafe(16),
    )
    return _sign_payload(payload.to_payload(), secret)


def create_estimate_context_token(
    *,
    secret: str,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    current_time = int(time.time() if now is None else now)
    payload = SpecificationContextToken(
        purpose=PURPOSE_CREATE_ESTIMATE,
        customer_id=int(customer_id),
        telegram_user_id=int(telegram_user_id),
        origin_chat_id=int(origin_chat_id),
        exp=current_time + int(ttl_seconds),
        nonce=secrets.token_urlsafe(16),
    )
    return _sign_payload(payload.to_payload(), secret)


def create_specification_edit_context_token(
    *,
    secret: str,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    current_time = int(time.time() if now is None else now)
    payload = SpecificationContextToken(
        purpose=PURPOSE_EDIT_SPECIFICATION,
        customer_id=int(customer_id),
        telegram_user_id=int(telegram_user_id),
        origin_chat_id=int(origin_chat_id),
        exp=current_time + int(ttl_seconds),
        nonce=secrets.token_urlsafe(16),
    )
    return _sign_payload(payload.to_payload(), secret)


def create_customer_edit_context_token(
    *,
    secret: str,
    customer_id: int,
    telegram_user_id: int,
    origin_chat_id: int,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    current_time = int(time.time() if now is None else now)
    payload = SpecificationContextToken(
        purpose=PURPOSE_EDIT_CUSTOMER,
        customer_id=int(customer_id),
        telegram_user_id=int(telegram_user_id),
        origin_chat_id=int(origin_chat_id),
        exp=current_time + int(ttl_seconds),
        nonce=secrets.token_urlsafe(16),
    )
    return _sign_payload(payload.to_payload(), secret)


def verify_specification_context_token(
    token: str,
    *,
    secret: str,
    expected_telegram_user_id: int | None = None,
    now: int | None = None,
) -> SpecificationContextToken:
    return _verify_context_token(
        token,
        secret=secret,
        expected_telegram_user_id=expected_telegram_user_id,
        now=now,
        expected_purpose=PURPOSE_CREATE_SPECIFICATION,
    )


def verify_estimate_context_token(
    token: str,
    *,
    secret: str,
    expected_telegram_user_id: int | None = None,
    now: int | None = None,
) -> SpecificationContextToken:
    return _verify_context_token(
        token,
        secret=secret,
        expected_telegram_user_id=expected_telegram_user_id,
        now=now,
        expected_purpose=PURPOSE_CREATE_ESTIMATE,
    )


def verify_specification_edit_context_token(
    token: str,
    *,
    secret: str,
    expected_telegram_user_id: int | None = None,
    now: int | None = None,
) -> SpecificationContextToken:
    return _verify_context_token(
        token,
        secret=secret,
        expected_telegram_user_id=expected_telegram_user_id,
        now=now,
        expected_purpose=PURPOSE_EDIT_SPECIFICATION,
    )


def verify_customer_edit_context_token(
    token: str,
    *,
    secret: str,
    expected_telegram_user_id: int | None = None,
    now: int | None = None,
) -> SpecificationContextToken:
    return _verify_context_token(
        token,
        secret=secret,
        expected_telegram_user_id=expected_telegram_user_id,
        now=now,
        expected_purpose=PURPOSE_EDIT_CUSTOMER,
    )


def _verify_context_token(
    token: str,
    *,
    secret: str,
    expected_telegram_user_id: int | None,
    now: int | None,
    expected_purpose: str,
) -> SpecificationContextToken:
    if not token or "." not in token:
        raise TokenError("INVALID_CONTEXT_TOKEN", "Контекстный токен повреждён")

    payload_part, signature_part = token.rsplit(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_part)
        provided_signature = _b64url_decode(signature_part)
    except (ValueError, TypeError) as error:
        raise TokenError("INVALID_CONTEXT_TOKEN", "Контекстный токен повреждён") from error

    expected_signature = _sign_bytes(payload_bytes, secret)
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise TokenError("INVALID_CONTEXT_TOKEN", "Подпись контекстного токена недействительна")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenError("INVALID_CONTEXT_TOKEN", "Контекстный токен повреждён") from error

    if not isinstance(payload, dict):
        raise TokenError("INVALID_CONTEXT_TOKEN", "Контекстный токен повреждён")

    purpose = payload.get("purpose")
    if purpose != expected_purpose:
        raise TokenError("INVALID_CONTEXT_TOKEN", "Неверное назначение токена")

    try:
        context = SpecificationContextToken(
            purpose=str(purpose),
            customer_id=int(payload["customer_id"]),
            telegram_user_id=int(payload["telegram_user_id"]),
            origin_chat_id=int(payload["origin_chat_id"]),
            exp=int(payload["exp"]),
            nonce=str(payload["nonce"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TokenError("INVALID_CONTEXT_TOKEN", "Контекстный токен повреждён") from error

    current_time = int(time.time() if now is None else now)
    if context.exp < current_time:
        raise TokenError("EXPIRED_CONTEXT_TOKEN", "Срок действия контекстного токена истёк")

    if (
        expected_telegram_user_id is not None
        and int(expected_telegram_user_id) != context.telegram_user_id
    ):
        raise TokenError("USER_MISMATCH", "Токен принадлежит другому пользователю")

    return context


def _sign_payload(payload: dict[str, Any], secret: str) -> str:
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_part = _b64url_encode(payload_bytes)
    signature_part = _b64url_encode(_sign_bytes(payload_bytes, secret))
    return f"{payload_part}.{signature_part}"


def _sign_bytes(payload_bytes: bytes, secret: str) -> bytes:
    return hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).digest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
