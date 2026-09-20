from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelAction:
    action: str
    session_id: str
    channel: str
    exp: int
    customer_id: str | None = None


def create_channel_action_token(*, secret: str, action: str, session_id: str, channel: str, ttl_seconds: int = 900, customer_id: str | None = None) -> str:
    payload = {
        "action": action,
        "session_id": str(session_id),
        "channel": channel,
        "exp": int(time.time()) + int(ttl_seconds),
        "nonce": secrets.token_urlsafe(8),
    }
    if customer_id is not None:
        payload["customer_id"] = str(customer_id)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded}.{signed}"


def verify_channel_action_token(token: str, *, secret: str, expected_channel: str, now: int | None = None) -> ChannelAction:
    try:
        encoded, signature = token.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        provided = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        payload = json.loads(raw.decode())
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("invalid action token")
    if not hmac.compare_digest(provided, expected) or payload.get("channel") != expected_channel:
        raise ValueError("invalid action token")
    if int(payload.get("exp", 0)) < int(time.time() if now is None else now):
        raise ValueError("expired action token")
    return ChannelAction(
        action=str(payload["action"]),
        session_id=str(payload["session_id"]),
        channel=str(payload["channel"]),
        exp=int(payload["exp"]),
        customer_id=str(payload["customer_id"]) if payload.get("customer_id") is not None else None,
    )
