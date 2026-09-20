from __future__ import annotations

import json
import logging
from pathlib import Path
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Document, Message, PhotoSize, Update, User

from app.bot import create_dispatcher
from app.config import Settings
from app.handlers.n8n_chat import N8N_UNAVAILABLE_USER_TEXT
from app.handlers.n8n_chat import N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT
from app.services.channel_event import (
    CUSTOMER_DOCUMENT_UPLOADED,
    CUSTOMER_ADD,
    CUSTOMER_DELETE,
    CUSTOMER_SEARCH_EDIT,
    DOCUMENT_RECOGNIZE,
    INTAKE_CANCEL,
    INTAKE_FINISH,
    INTAKE_START,
    SPECIFICATION_ADD,
    ChannelEvent,
    build_telegram_attachment_event,
    build_telegram_command_event,
    telegram_command_to_action,
)
from app.services.n8n_chat_service import (
    N8NChatError,
    N8NChatService,
    extract_n8n_assistant_text,
)
from tests.test_telegram_proxy import _settings as _base_settings


WEBHOOK_URL = "https://n8n.example/webhook/telegram"
WEBHOOK_SECRET = "n8n-test-secret-value"
ASSISTANT_TEXT = "Ответ ассистента"
BOT_TOKEN = "123456:TESTTOKEN"


def _settings(**overrides) -> Settings:
    values = {
        "n8n_telegram_webhook_url": WEBHOOK_URL,
        "n8n_webhook_secret": WEBHOOK_SECRET,
    }
    values.update(overrides)
    return _base_settings(**values)


def _gpt_payload(text: str = ASSISTANT_TEXT) -> dict:
    return {
        "result": {
            "alternatives": [
                {
                    "message": {
                        "role": "assistant",
                        "text": text,
                    }
                }
            ]
        }
    }


def _message(
    *,
    text: str,
    chat_type: ChatType = ChatType.PRIVATE,
    message_id: int = 7,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=42, type=chat_type),
        from_user=User(id=42, is_bot=False, first_name="Ivan"),
        text=text,
    )


def _photo_message(
    *,
    message_id: int = 790,
    media_group_id: str | None = "album-123",
    caption: str | None = "Паспорт",
    chat_type: ChatType = ChatType.PRIVATE,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=456, type=chat_type),
        from_user=User(id=123, is_bot=False, first_name="Ivan"),
        photo=[
            PhotoSize(
                file_id="small-photo-file-id",
                file_unique_id="small-photo-unique-id",
                width=90,
                height=90,
                file_size=1000,
            ),
            PhotoSize(
                file_id="large-photo-file-id",
                file_unique_id="large-photo-unique-id",
                width=1280,
                height=960,
                file_size=123456,
            ),
        ],
        media_group_id=media_group_id,
        caption=caption,
    )


def _document_message(
    *,
    file_name: str | None = "passport.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int | None = 345678,
    message_id: int = 791,
    media_group_id: str | None = None,
    caption: str | None = None,
    chat_type: ChatType = ChatType.PRIVATE,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=456, type=chat_type),
        from_user=User(id=123, is_bot=False, first_name="Ivan"),
        document=Document(
            file_id="document-file-id",
            file_unique_id="document-unique-id",
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        ),
        media_group_id=media_group_id,
        caption=caption,
    )


def _update(message: Message) -> Update:
    return Update(update_id=1, message=message)


def _sent_text(bot_call: AsyncMock) -> str | None:
    if not bot_call.await_args:
        return None
    method = bot_call.await_args.args[0]
    return getattr(method, "text", None)


class ExtractN8NAssistantTextTests(unittest.TestCase):
    def test_intake_workflow_summary_formats_review_fields_safely(self) -> None:
        workflow = json.loads(
            (Path(__file__).parents[1] / "n8n" / "WF_router_FINAL.json").read_text()
        )
        code_nodes = [node for node in workflow["nodes"] if node["name"] == "Format Intake Review Reply"]
        self.assertEqual(len(code_nodes), 1)
        code = code_nodes[0]["parameters"]["jsCode"]
        self.assertIn("fields.birth_place", code)
        self.assertIn("fields.by_whom_issued", code)
        self.assertIn("valueOrNotRecognized", code)
        self.assertIn("Место рождения", code)
        self.assertIn("Кем выдан", code)
    def test_extracts_assistant_text(self) -> None:
        self.assertEqual(
            extract_n8n_assistant_text(_gpt_payload("  hello  ")),
            "hello",
        )

    def test_extracts_normalized_n8n_reply_text(self) -> None:
        self.assertEqual(
            extract_n8n_assistant_text(
                {"ok": True, "reply": {"type": "text", "text": "  hello  "}}
            ),
            "hello",
        )

    def test_malformed_payload_raises(self) -> None:
        cases = [
            None,
            [],
            {},
            {"result": {}},
            {"result": {"alternatives": []}},
            {"result": {"alternatives": ["x"]}},
            {"result": {"alternatives": [{"message": {}}]}},
            {"result": {"alternatives": [{"message": {"text": ""}}]}},
            {"result": {"alternatives": [{"message": {"text": "   "}}]}},
            {"result": {"alternatives": [{"message": {"text": 123}}]}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(N8NChatError):
                    extract_n8n_assistant_text(payload)


class ChannelEventTests(unittest.TestCase):
    def test_telegram_command_actions_are_centralized(self) -> None:
        cases = {
            "/add_customer": CUSTOMER_ADD,
            "/search_edit_customer": CUSTOMER_SEARCH_EDIT,
            "/delete_customer": CUSTOMER_DELETE,
            "/add_specification": SPECIFICATION_ADD,
            "/recognize_document": DOCUMENT_RECOGNIZE,
            "/add_customer@AutoExportBot": CUSTOMER_ADD,
        }
        for command, action in cases.items():
            with self.subTest(command=command):
                self.assertEqual(telegram_command_to_action(command), action)

        self.assertIsNone(telegram_command_to_action("/add_customer_legacy"))

    def test_builds_telegram_command_event_without_dispatcher_wiring(self) -> None:
        event = build_telegram_command_event(_message(text="/add_customer"))

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(
            event.to_dict(),
            {
                "version": 1,
                "channel": "telegram",
                "type": "command",
                "action": CUSTOMER_ADD,
                "payload": {},
                "attachments": [],
                "metadata": {
                    "user_id": "42",
                    "chat_id": "42",
                    "message_id": "7",
                },
            },
        )

    def test_contract_supports_future_bitrix_channel(self) -> None:
        event = ChannelEvent(channel="bitrix", type="callback")

        self.assertEqual(
            event.to_dict(),
            {
                "version": 1,
                "channel": "bitrix",
                "type": "callback",
                "action": None,
                "payload": {},
                "attachments": [],
                "metadata": {},
            },
        )

    def test_builds_telegram_attachment_event(self) -> None:
        event = build_telegram_attachment_event(
            file_id="AgACAgIAAxkBA",
            file_unique_id="AQADunique",
            name="photo_790.jpg",
            mime_type="image/jpeg",
            kind="photo",
            size=123456,
            chat_id="456",
            user_id="123",
            message_id="790",
            media_group_id="12345678901234567",
            caption="Паспорт",
        )

        self.assertEqual(
            event.to_dict(),
            {
                "version": 1,
                "channel": "telegram",
                "type": "attachment",
                "action": CUSTOMER_DOCUMENT_UPLOADED,
                "payload": {},
                "attachments": [
                    {
                        "id": "AgACAgIAAxkBA",
                        "name": "photo_790.jpg",
                        "mime_type": "image/jpeg",
                        "kind": "photo",
                        "size": 123456,
                    }
                ],
                "metadata": {
                    "user_id": "123",
                    "chat_id": "456",
                    "message_id": "790",
                    "media_group_id": "12345678901234567",
                    "telegram_file_unique_id": "AQADunique",
                    "caption": "Паспорт",
                },
            },
        )


class N8NChatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def _send(
        self,
        handler,
        *,
        text: str = "Привет",
    ) -> str:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = N8NChatService(
            webhook_url=WEBHOOK_URL,
            webhook_secret=WEBHOOK_SECRET,
            client=client,
        )
        try:
            return await service.send_telegram_text(
                text=text,
                chat_id="42",
                user_id="42",
                message_id="7",
            )
        finally:
            await client.aclose()

    async def test_posts_expected_payload_and_header(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["secret"] = request.headers.get("X-N8N-Webhook-Secret")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_gpt_payload())

        reply = await self._send(handler)
        self.assertEqual(reply, ASSISTANT_TEXT)
        self.assertEqual(captured["url"], WEBHOOK_URL)
        self.assertEqual(captured["secret"], WEBHOOK_SECRET)
        self.assertEqual(
            captured["body"],
            {
                "version": 1,
                "channel": "telegram",
                "type": "text",
                "action": None,
                "payload": {
                    "text": "Привет",
                },
                "attachments": [],
                "metadata": {
                    "user_id": "42",
                    "chat_id": "42",
                    "message_id": "7",
                },
            },
        )
        self.assertNotIn("text", captured["body"])

    async def test_posts_attachment_payload(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_gpt_payload())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = N8NChatService(
            webhook_url=WEBHOOK_URL,
            webhook_secret=WEBHOOK_SECRET,
            client=client,
        )
        try:
            reply = await service.send_telegram_attachment(
                file_id="large-photo-file-id",
                file_unique_id="large-photo-unique-id",
                name="photo_790.jpg",
                mime_type="image/jpeg",
                kind="photo",
                size=123456,
                chat_id="456",
                user_id="123",
                message_id="790",
                media_group_id="album-123",
                caption="Паспорт",
            )
        finally:
            await client.aclose()

        self.assertEqual(reply, ASSISTANT_TEXT)
        self.assertEqual(captured["body"]["action"], CUSTOMER_DOCUMENT_UPLOADED)
        self.assertEqual(
            captured["body"]["attachments"][0],
            {
                "id": "large-photo-file-id",
                "name": "photo_790.jpg",
                "mime_type": "image/jpeg",
                "kind": "photo",
                "size": 123456,
            },
        )
        self.assertEqual(captured["body"]["metadata"]["media_group_id"], "album-123")

    async def test_posts_intake_command_events(self) -> None:
        captured_actions: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_actions.append(json.loads(request.content)["action"])
            return httpx.Response(200, json=_gpt_payload())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = N8NChatService(
            webhook_url=WEBHOOK_URL,
            webhook_secret=WEBHOOK_SECRET,
            client=client,
        )
        try:
            for command in ("/intake_start", "/intake_finish", "/intake_cancel"):
                await service.send_telegram_command(_message(text=command))
        finally:
            await client.aclose()

        self.assertEqual(captured_actions, [INTAKE_START, INTAKE_FINISH, INTAKE_CANCEL])

    async def test_http_error_raises_service_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        with self.assertRaisesRegex(N8NChatError, "502"):
            await self._send(handler)

    async def test_network_error_raises_service_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with self.assertRaises(N8NChatError):
            await self._send(handler)

    async def test_invalid_json_raises_service_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json")

        with self.assertRaises(N8NChatError):
            await self._send(handler)

    async def test_malformed_n8n_json_raises_service_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"alternatives": []}})

        with self.assertRaises(N8NChatError):
            await self._send(handler)

    async def test_secret_is_not_logged_on_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal")

        with self.assertLogs("app.services.n8n_chat_service", level="ERROR") as captured:
            with self.assertRaises(N8NChatError):
                await self._send(handler)

        combined = "\n".join(captured.output)
        self.assertNotIn(WEBHOOK_SECRET, combined)


class N8NChatHandlerTests(unittest.IsolatedAsyncioTestCase):
    dispatcher = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.dispatcher = create_dispatcher()

    async def asyncSetUp(self) -> None:
        self.bot = Bot(token=BOT_TOKEN)
        self.bot_call = AsyncMock(return_value=True)
        self._call_patcher = patch.object(Bot, "__call__", self.bot_call)
        self._call_patcher.start()

    async def asyncTearDown(self) -> None:
        self._call_patcher.stop()
        for chat_id, user_id in ((42, 42), (456, 123)):
            key = StorageKey(bot_id=self.bot.id, chat_id=chat_id, user_id=user_id)
            await self.dispatcher.storage.set_state(key, None)
            await self.dispatcher.storage.set_data(key, {})
        await self.bot.session.close()

    async def _feed(self, message: Message, *, settings: Settings | None = None) -> None:
        customer_upload_batch_repository = AsyncMock()
        customer_upload_batch_repository.get_active_batch.return_value = None
        await self.dispatcher.feed_update(
            self.bot,
            _update(message),
            settings=settings or _settings(),
            database=AsyncMock(),
            customer_upload_batch_repository=customer_upload_batch_repository,
        )

    async def test_private_text_calls_n8n_and_replies(self) -> None:
        send_telegram_text = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_text",
            send_telegram_text,
        ):
            await self._feed(_message(text="Сколько стоит доставка?"))

        send_telegram_text.assert_awaited_once_with(
            text="Сколько стоит доставка?",
            chat_id="42",
            user_id="42",
            message_id="7",
        )
        self.assertEqual(_sent_text(self.bot_call), ASSISTANT_TEXT)

    async def test_web_app_reply_creates_telegram_button(self) -> None:
        reply = {
            "type": "web_app",
            "text": "Распознанные данные клиента",
            "button_text": "✏️ Ручная коррекция",
            "url": "https://api.ulkar.ru/miniapp/customer?token=signed",
        }
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_command",
            AsyncMock(return_value=reply),
        ):
            await self._feed(_message(text="/intake_finish"))

        method = self.bot_call.await_args.args[0]
        self.assertEqual(method.text, reply["text"])
        button = method.reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.text, reply["button_text"])
        self.assertEqual(button.web_app.url, reply["url"])

    async def test_slash_command_is_not_sent_to_n8n(self) -> None:
        send_telegram_text = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_text",
            send_telegram_text,
        ):
            await self._feed(_message(text="/start"))

        send_telegram_text.assert_not_awaited()
        sent = _sent_text(self.bot_call)
        self.assertIsNotNone(sent)
        self.assertNotEqual(sent, ASSISTANT_TEXT)
        self.assertNotEqual(sent, N8N_UNAVAILABLE_USER_TEXT)

    async def test_group_message_is_not_sent_to_n8n(self) -> None:
        send_telegram_text = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_text",
            send_telegram_text,
        ):
            await self._feed(
                _message(text="Привет всем", chat_type=ChatType.GROUP)
            )

        send_telegram_text.assert_not_awaited()
        self.bot_call.assert_not_awaited()

    async def test_active_fsm_state_skips_n8n_handler(self) -> None:
        key = StorageKey(bot_id=self.bot.id, chat_id=42, user_id=42)
        await self.dispatcher.storage.set_state(
            key,
            "UnrelatedStates:waiting_value",
        )
        send_telegram_text = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_text",
            send_telegram_text,
        ):
            await self._feed(_message(text="Иванов Иван"))

        send_telegram_text.assert_not_awaited()
        self.bot_call.assert_not_awaited()

    async def test_http_error_returns_user_fallback(self) -> None:
        send_telegram_text = AsyncMock(
            side_effect=N8NChatError("n8n webhook returned HTTP 502")
        )
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_text",
            send_telegram_text,
        ):
            with self.assertLogs("app.handlers.n8n_chat", level="ERROR") as captured:
                await self._feed(_message(text="Привет"))

        send_telegram_text.assert_awaited_once()
        self.assertEqual(_sent_text(self.bot_call), N8N_UNAVAILABLE_USER_TEXT)
        self.assertNotIn(WEBHOOK_SECRET, "\n".join(captured.output))

    async def test_private_photo_calls_n8n_and_replies(self) -> None:
        send_telegram_attachment = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_attachment",
            send_telegram_attachment,
        ):
            await self._feed(_photo_message())

        send_telegram_attachment.assert_awaited_once_with(
            file_id="large-photo-file-id",
            file_unique_id="large-photo-unique-id",
            name="photo_790.jpg",
            mime_type="image/jpeg",
            kind="photo",
            size=123456,
            chat_id="456",
            user_id="123",
            message_id="790",
            media_group_id="album-123",
            caption="Паспорт",
        )
        self.assertEqual(_sent_text(self.bot_call), ASSISTANT_TEXT)

    async def test_private_document_calls_n8n_and_replies(self) -> None:
        send_telegram_attachment = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_attachment",
            send_telegram_attachment,
        ):
            await self._feed(_document_message(caption="PDF"))

        send_telegram_attachment.assert_awaited_once_with(
            file_id="document-file-id",
            file_unique_id="document-unique-id",
            name="passport.pdf",
            mime_type="application/pdf",
            kind="document",
            size=345678,
            chat_id="456",
            user_id="123",
            message_id="791",
            media_group_id=None,
            caption="PDF",
        )
        self.assertEqual(_sent_text(self.bot_call), ASSISTANT_TEXT)

    async def test_disabled_n8n_does_not_send_attachment(self) -> None:
        send_telegram_attachment = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_attachment",
            send_telegram_attachment,
        ):
            await self._feed(
                _photo_message(),
                settings=_settings(
                    n8n_telegram_webhook_url=None,
                    n8n_webhook_secret=None,
                ),
            )

        send_telegram_attachment.assert_not_awaited()
        self.bot_call.assert_not_awaited()

    async def test_unsupported_document_is_not_sent_to_n8n(self) -> None:
        send_telegram_attachment = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_attachment",
            send_telegram_attachment,
        ):
            await self._feed(
                _document_message(
                    file_name="archive.zip",
                    mime_type="application/zip",
                    file_size=100,
                )
            )

        send_telegram_attachment.assert_not_awaited()
        self.assertEqual(_sent_text(self.bot_call), N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT)

    async def test_active_fsm_state_skips_n8n_attachment_handler(self) -> None:
        key = StorageKey(bot_id=self.bot.id, chat_id=456, user_id=123)
        await self.dispatcher.storage.set_state(
            key,
            "CustomerStates:waiting_passport",
        )
        send_telegram_attachment = AsyncMock(return_value=ASSISTANT_TEXT)
        with patch(
            "app.handlers.n8n_chat.N8NChatService.send_telegram_attachment",
            send_telegram_attachment,
        ):
            await self._feed(_photo_message())

        send_telegram_attachment.assert_not_awaited()
        self.bot_call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
