from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChannelName = Literal["telegram", "max", "wechat"]
ChannelEventType = Literal["text", "command", "attachment", "callback"]
AttachmentKind = Literal["document", "photo", "video"]

CUSTOMER_ADD = "customer.add"
CUSTOMER_SEARCH_EDIT = "customer.search_edit"
CUSTOMER_DELETE = "customer.delete"
SPECIFICATION_ADD = "specification.add"
DOCUMENT_RECOGNIZE = "document.recognize"

TELEGRAM_COMMAND_ACTIONS: dict[str, str] = {
    "/add_customer": CUSTOMER_ADD,
    "/search_edit_customer": CUSTOMER_SEARCH_EDIT,
    "/delete_customer": CUSTOMER_DELETE,
    "/add_specification": SPECIFICATION_ADD,
    "/recognize_document": DOCUMENT_RECOGNIZE,
}


@dataclass(frozen=True)
class ChannelAttachment:
    id: str
    name: str
    mime_type: str
    kind: AttachmentKind
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mime_type": self.mime_type,
            "kind": self.kind,
            "size": self.size,
        }


@dataclass(frozen=True)
class ChannelEvent:
    channel: ChannelName
    user_id: str
    chat_id: str
    message_id: str
    type: ChannelEventType
    version: int = 1
    action: str | None = None
    text: str | None = None
    attachments: list[ChannelAttachment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "channel": self.channel,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "type": self.type,
            "action": self.action,
            "text": self.text,
            "attachments": [
                attachment.to_dict() for attachment in self.attachments
            ],
            "metadata": dict(self.metadata),
        }


def telegram_command_to_action(command: str) -> str | None:
    normalized = command.strip().split(maxsplit=1)[0].split("@", 1)[0]
    return TELEGRAM_COMMAND_ACTIONS.get(normalized)


def build_telegram_text_event(
    *,
    text: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    metadata: dict[str, Any] | None = None,
) -> ChannelEvent:
    return ChannelEvent(
        channel="telegram",
        user_id=str(user_id),
        chat_id=str(chat_id),
        message_id=str(message_id),
        type="text",
        text=text,
        metadata={} if metadata is None else dict(metadata),
    )


def build_telegram_command_event(message: Any) -> ChannelEvent | None:
    text = (getattr(message, "text", None) or "").strip()
    if not text.startswith("/"):
        return None

    action = telegram_command_to_action(text)
    if action is None:
        return None

    chat = getattr(message, "chat", None)
    from_user = getattr(message, "from_user", None)
    return ChannelEvent(
        channel="telegram",
        user_id="" if from_user is None else str(getattr(from_user, "id", "")),
        chat_id="" if chat is None else str(getattr(chat, "id", "")),
        message_id=str(getattr(message, "message_id", "") or ""),
        type="command",
        action=action,
        text=None,
        attachments=[],
        metadata={},
    )
