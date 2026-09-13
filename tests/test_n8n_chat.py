from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Message, Update, User

from app.bot import create_dispatcher
from app.config import Settings
from app.handlers.n8n_chat import N8N_UNAVAILABLE_USER_TEXT
from app.services.channel_event import (
    CUSTOMER_ADD,
    CUSTOMER_DELETE,
    CUSTOMER_SEARCH_EDIT,
    DOCUMENT_RECOGNIZE,
    SPECIFICATION_ADD,
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


def _update(message: Message) -> Update:
    return Update(update_id=1, message=message)


def _sent_text(bot_call: AsyncMock) -> str | None:
    if not bot_call.await_args:
        return None
    method = bot_call.await_args.args[0]
    return getattr(method, "text", None)


class ExtractN8NAssistantTextTests(unittest.TestCase):
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
                "user_id": "42",
                "chat_id": "42",
                "message_id": "7",
                "type": "command",
                "action": CUSTOMER_ADD,
                "text": None,
                "attachments": [],
                "metadata": {},
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
                "user_id": "42",
                "chat_id": "42",
                "message_id": "7",
                "type": "text",
                "action": None,
                "text": "Привет",
                "attachments": [],
                "metadata": {},
            },
        )

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
        key = StorageKey(bot_id=self.bot.id, chat_id=42, user_id=42)
        await self.dispatcher.storage.set_state(key, None)
        await self.dispatcher.storage.set_data(key, {})
        await self.bot.session.close()

    async def _feed(self, message: Message, *, settings: Settings | None = None) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            _update(message),
            settings=settings or _settings(),
            database=AsyncMock(),
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


if __name__ == "__main__":
    unittest.main()
