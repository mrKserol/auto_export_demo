from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ReviewCardRef:
    channel: str
    conversation_id: str
    message_id: str


def review_card_ref_from_metadata(metadata: dict) -> ReviewCardRef | None:
    ref = metadata.get("review_card_ref") or metadata.get("review_message")
    if not isinstance(ref, dict) or not ref.get("message_id"):
        return None
    return ReviewCardRef(
        channel=str(ref.get("channel") or "telegram"),
        conversation_id=str(ref.get("conversation_id") or ref.get("chat_id") or ""),
        message_id=str(ref["message_id"]),
    )


class ChannelAdapter(Protocol):
    async def send_card(self, *, conversation_id: str, text: str, keyboard: Any) -> ReviewCardRef: ...
    async def edit_card(self, *, ref: ReviewCardRef, text: str, keyboard: Any) -> None: ...
    async def answer_action(self, *, action_id: str, text: str, alert: bool = False) -> None: ...
    def build_open_form_action(self, *, url: str, text: str) -> dict[str, str]: ...


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, bot) -> None:
        self.bot = bot

    async def send_card(self, *, conversation_id: str, text: str, keyboard: Any) -> ReviewCardRef:
        message = await self.bot.send_message(
            chat_id=int(conversation_id),
            text=text,
            reply_markup=keyboard,
        )
        return ReviewCardRef(self.channel, str(message.chat.id), str(message.message_id))

    async def edit_card(self, *, ref: ReviewCardRef, text: str, keyboard: Any) -> None:
        await self.bot.edit_message_text(
            chat_id=int(ref.conversation_id),
            message_id=int(ref.message_id),
            text=text,
            reply_markup=keyboard,
        )

    async def answer_action(self, *, action_id: str, text: str, alert: bool = False) -> None:
        await self.bot.answer_callback_query(action_id, text=text, show_alert=alert)

    def build_open_form_action(self, *, url: str, text: str) -> dict[str, str]:
        return {"type": "web_app", "text": text, "url": url}


class ExternalUrlAdapter:
    """Neutral fallback for channels without a native embedded form."""

    channel = "external"

    def build_open_form_action(self, *, url: str, text: str) -> dict[str, str]:
        return {"type": "url", "text": text, "url": url}
