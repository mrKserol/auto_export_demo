from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, WebAppInfo

from app.config import Settings
from app.handlers.customer_batch_upload import handle_batch_edit
from app.handlers.start import _handle_edit_batch_deep_link
from app.repositories.customer_upload_batch_statuses import CustomerUploadBatchStatus
from app.services.miniapp_entrypoint_service import (
    PURPOSE_EDIT_BATCH,
    send_miniapp_entrypoint,
)
from app.services.miniapp_link_service import create_customer_batch_token
from app.web.token_service import verify_customer_batch_context_token


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="1:TEST",
        database_url="postgresql://unused",
        yandex_disk_token="token",
        yandex_disk_base_path="/auto_export_demo",
        yandex_function_url=None,
        enable_processing=False,
        yandex_api_key="key",
        yandex_cloud_folder_id="folder",
        ocr_min_delay_seconds=1.5,
        max_ocr_retries=3,
        mini_app_base_url="https://example.com",
        mini_app_token_secret="secret",
        web_host="0.0.0.0",
        web_port=8000,
        mini_app_token_ttl_seconds=900,
        telegram_init_data_max_age_seconds=900,
        customer_upload_max_file_bytes=1024,
    )


def _callback(*, chat_id: int = -100123, chat_type: str = "supergroup", user_id: int = 42):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = MagicMock()
    callback.message.chat = SimpleNamespace(id=chat_id, type=chat_type)
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _mock_state(batch_id: int) -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"batch_id": batch_id})
    state.update_data = AsyncMock()
    return state


class MiniAppGroupEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_chat_gets_web_app_button(self) -> None:
        bot = AsyncMock()
        database = AsyncMock()
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=1
        )
        result = await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="private",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/miniapp/customer?token=abc",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="group ok",
            deep_link_group_text="open bot",
        )
        self.assertEqual(result.channel, "private")
        markup = answer.await_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertIsInstance(button.web_app, WebAppInfo)
        bot.send_message.assert_not_awaited()
        database.create_mini_app_launch_code.assert_not_awaited()

    async def test_supergroup_does_not_get_web_app_button(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock()
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="opaqueCode")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="supergroup",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/miniapp/customer?token=abc",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="Форма ручной коррекции отправлена вам в личные сообщения.",
            deep_link_group_text="Чтобы открыть форму, сначала перейдите в личный чат с ботом:",
        )
        group_markup = answer.await_args.kwargs.get("reply_markup")
        self.assertIsNone(group_markup)
        for call in answer.await_args_list:
            markup = call.kwargs.get("reply_markup")
            if markup is None:
                continue
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertIsNone(getattr(button, "web_app", None))

    async def test_supergroup_sends_form_to_private_chat(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock()
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="opaqueCode")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        result = await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="supergroup",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/miniapp/customer?token=abc",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="Форма ручной коррекции отправлена вам в личные сообщения.",
            deep_link_group_text="open bot",
        )
        self.assertEqual(result.channel, "dm")
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 42)
        self.assertIn(
            "личные сообщения",
            answer.await_args.args[0],
        )

    async def test_private_message_contains_web_app_button(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock()
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="opaqueCode")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="supergroup",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/miniapp/customer?token=abc",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="ok",
            deep_link_group_text="open bot",
        )
        dm_markup = bot.send_message.await_args.kwargs["reply_markup"]
        button = dm_markup.inline_keyboard[0][0]
        self.assertEqual(button.text, "📝 Открыть данные клиента")
        self.assertIsInstance(button.web_app, WebAppInfo)

    async def test_forbidden_dm_returns_deep_link(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(
                method=MagicMock(),
                message="Forbidden: bot can't initiate conversation with a user",
            )
        )
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="opaqueToken12")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        result = await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="supergroup",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/miniapp/customer?token=abc",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="ok",
            deep_link_group_text=(
                "Чтобы открыть форму, сначала перейдите в личный чат с ботом:"
            ),
        )
        self.assertEqual(result.channel, "deep_link")
        self.assertEqual(result.launch_code, "opaqueToken12")
        text = answer.await_args.args[0]
        self.assertIn("личный чат с ботом", text)
        markup = answer.await_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, "Открыть бота")
        self.assertEqual(
            button.url,
            "https://t.me/demo_bot?start=edit_batch_opaqueToken12",
        )
        self.assertIsNone(getattr(button, "web_app", None))

    async def test_deep_link_token_has_no_pii(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden")
        )
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="xY9_opaque")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="supergroup",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url=f"https://example.com/miniapp/customer?token={token}",
            open_button_text="📝 Открыть данные клиента",
            private_text="private text",
            group_success_text="ok",
            deep_link_group_text="open bot",
        )
        button = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertNotIn("Иван", button.url)
        self.assertNotIn("Ivanov", button.url)
        self.assertNotIn(token, button.url)
        self.assertNotIn("batch_id=7", button.url)
        self.assertIn("edit_batch_xY9_opaque", button.url)

    async def test_foreign_user_cannot_use_token(self) -> None:
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        database = AsyncMock()
        database.consume_mini_app_launch_code = AsyncMock(return_value=None)
        message = MagicMock()
        message.from_user = SimpleNamespace(id=999)
        message.chat = SimpleNamespace(id=999, type="private")
        message.answer = AsyncMock()
        await _handle_edit_batch_deep_link(
            message=message,
            launch_code="opaque",
            settings=settings,
            database=database,
        )
        self.assertIn("недействительна", message.answer.await_args.args[0])
        # Owner can consume and open form.
        database.consume_mini_app_launch_code = AsyncMock(
            return_value={
                "context_token": token,
                "telegram_user_id": 42,
                "purpose": PURPOSE_EDIT_BATCH,
            }
        )
        message.from_user = SimpleNamespace(id=42)
        message.answer.reset_mock()
        await _handle_edit_batch_deep_link(
            message=message,
            launch_code="opaque",
            settings=settings,
            database=database,
        )
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup.inline_keyboard[0][0].web_app, WebAppInfo)

    async def test_telegram_error_is_handled(self) -> None:
        bot = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="demo_bot"))
        bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden")
        )
        database = AsyncMock()
        database.create_mini_app_launch_code = AsyncMock(return_value="code")
        answer = AsyncMock()
        settings = _settings()
        token = create_customer_batch_token(
            settings, batch_id=7, telegram_user_id=42, origin_chat_id=-100
        )
        result = await send_miniapp_entrypoint(
            bot,
            database=database,
            settings=settings,
            user_id=42,
            origin_chat_type="group",
            answer=answer,
            purpose=PURPOSE_EDIT_BATCH,
            entity_id=7,
            context_token=token,
            miniapp_url="https://example.com/x",
            open_button_text="open",
            private_text="private",
            group_success_text="ok",
            deep_link_group_text="deep",
        )
        self.assertEqual(result.channel, "deep_link")
        answer.assert_awaited()

    async def test_callback_answer_called_for_batch_edit(self) -> None:
        callback = _callback(chat_type="private", chat_id=10, user_id=20)
        state = _mock_state(5)
        repo = AsyncMock()
        repo.get_batch_by_id = AsyncMock(
            return_value={
                "id": 5,
                "status": CustomerUploadBatchStatus.FILES_SAVED,
                "telegram_chat_id": 10,
                "telegram_user_id": 20,
            }
        )
        repo.mark_batch_awaiting_confirmation = AsyncMock()
        bot = AsyncMock()
        database = AsyncMock()
        with patch(
            "app.handlers.customer_batch_upload._resolve_batch_for_callback",
            AsyncMock(
                return_value={
                    "id": 5,
                    "status": CustomerUploadBatchStatus.FILES_SAVED,
                }
            ),
        ):
            await handle_batch_edit(
                callback,
                state,
                _settings(),
                repo,
                bot,
                database,
            )
        callback.answer.assert_awaited()

    async def test_existing_private_flow_still_works(self) -> None:
        callback = _callback(chat_type="private", chat_id=10, user_id=20)
        state = _mock_state(5)
        repo = AsyncMock()
        repo.mark_batch_awaiting_confirmation = AsyncMock()
        bot = AsyncMock()
        database = AsyncMock()
        with patch(
            "app.handlers.customer_batch_upload._resolve_batch_for_callback",
            AsyncMock(
                return_value={
                    "id": 5,
                    "status": CustomerUploadBatchStatus.FILES_SAVED,
                }
            ),
        ):
            await handle_batch_edit(
                callback,
                state,
                _settings(),
                repo,
                bot,
                database,
            )
        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertIsInstance(button, InlineKeyboardButton)
        self.assertIsInstance(button.web_app, WebAppInfo)
        self.assertTrue(
            button.web_app.url.startswith("https://example.com/miniapp/customer?token=")
        )
        context = verify_customer_batch_context_token(
            button.web_app.url.split("token=", 1)[1],
            secret="secret",
            expected_telegram_user_id=20,
        )
        self.assertEqual(context.batch_id, 5)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
