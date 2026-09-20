from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import secrets
from uuid import UUID


@dataclass
class CompactActionRecord:
    action: str
    session_id: UUID
    channel: str
    external_user_id: str
    conversation_id: str
    expires_at: float
    used: bool = False
    customer_id: str | None = None


_records: dict[str, CompactActionRecord] = {}


def create_compact_action_token(*, action: str, session_id: UUID | str,
                                channel: str, external_user_id: str,
                                conversation_id: str, ttl_seconds: int = 900,
                                customer_id: str | None = None) -> str:
    token = secrets.token_urlsafe(16)
    _records[hashlib.sha256(token.encode()).hexdigest()] = CompactActionRecord(
        action=action, session_id=UUID(str(session_id)), channel=channel,
        external_user_id=str(external_user_id), conversation_id=str(conversation_id),
        expires_at=datetime.now(timezone.utc).timestamp() + ttl_seconds,
        customer_id=customer_id,
    )
    return token


def consume_compact_action_token(token: str, *, action: str | None, channel: str,
                                  external_user_id: str, conversation_id: str,
                                  now: float | None = None) -> CompactActionRecord:
    record = _records.get(hashlib.sha256(token.encode()).hexdigest())
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if record is None or record.used or record.expires_at < current:
        raise ValueError("invalid or expired action token")
    if ((action is not None and record.action != action) or record.channel != channel or
            record.external_user_id != str(external_user_id) or
            record.conversation_id != str(conversation_id)):
        raise ValueError("action token owner mismatch")
    record.used = True
    return record


def telegram_callback_data(token: str) -> str:
    callback_data = f"a:{token}"
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return callback_data
