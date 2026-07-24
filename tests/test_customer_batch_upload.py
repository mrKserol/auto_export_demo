from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command

from app.config import Settings
from app.database import Database
from app.handlers import customer_add as customer_add_module
from app.handlers import customer_batch_upload as batch_module
from app.handlers.customer_batch_upload import (
    handle_add_customer_batch,
    handle_batch_cancel,
    handle_batch_document_in_state,
    handle_batch_document_restore,
    handle_batch_process,
    _batch_keyboard,
    _format_file_accepted_message,
)
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.services.customer_folder_service import FolderFinalizeResult
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_file_download_service import (
    CustomerFileDownloadError,
    download_customer_file_from_telegram,
)
from app.services.customer_batch_recognition_service import (
    CustomerBatchRecognitionService,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionResult,
)
from app.states.customer_states import CustomerAddStates, CustomerBatchUploadStates
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "1:TEST",
        "database_url": "postgresql://example",
        "yandex_disk_token": "token",
        "yandex_disk_base_path": "/base",
        "yandex_function_url": None,
        "enable_processing": False,
        "yandex_api_key": "key",
        "yandex_cloud_folder_id": "folder",
        "ocr_min_delay_seconds": 1.5,
        "max_ocr_retries": 5,
        "mini_app_base_url": "https://example.com",
        "mini_app_token_secret": "secret",
        "web_host": "0.0.0.0",
        "web_port": 8000,
        "mini_app_token_ttl_seconds": 900,
        "telegram_init_data_max_age_seconds": 900,
        "customer_upload_max_file_bytes": 1024,
    }
    values.update(overrides)
    return Settings(**values)


def _mock_state(initial: dict | None = None, state_name: str | None = None) -> AsyncMock:
    state = AsyncMock()
    data = dict(initial or {})
    current = {"value": state_name}

    async def get_data() -> dict:
        return dict(data)

    async def update_data(**kwargs) -> None:
        data.update(kwargs)

    async def set_state(value) -> None:
        current["value"] = value.state if hasattr(value, "state") else value

    async def get_state():
        return current["value"]

    async def clear() -> None:
        data.clear()
        current["value"] = None

    state.get_data = get_data
    state.update_data = update_data
    state.set_state = set_state
    state.get_state = get_state
    state.clear = clear
    state._data = data
    return state


def _mock_bot(content: bytes) -> AsyncMock:
    bot = AsyncMock()
    bot.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="files/doc.bin")
    )

    async def download_file(_path, destination):
        destination.write(content)

    bot.download_file = AsyncMock(side_effect=download_file)
    return bot


def _photo_message(
    *,
    message_id: int,
    chat_id: int = 100,
    user_id: int = 200,
    file_size: int = 8,
    media_group_id: str | None = None,
) -> MagicMock:
    message = MagicMock()
    message.message_id = message_id
    message.media_group_id = media_group_id
    message.document = None
    message.photo = [
        SimpleNamespace(file_id=f"small-{message_id}", file_size=1),
        SimpleNamespace(file_id=f"photo-{message_id}", file_size=file_size),
    ]
    message.chat = SimpleNamespace(id=chat_id)
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    return message


def _document_message(
    *,
    message_id: int,
    filename: str = "doc.pdf",
    mime_type: str = "application/pdf",
    chat_id: int = 100,
    user_id: int = 200,
    file_size: int = 12,
    media_group_id: str | None = None,
) -> MagicMock:
    message = MagicMock()
    message.message_id = message_id
    message.media_group_id = media_group_id
    message.photo = None
    message.document = SimpleNamespace(
        file_id=f"doc-{message_id}",
        file_name=filename,
        mime_type=mime_type,
        file_size=file_size,
        file_unique_id=f"uniq-{message_id}",
    )
    message.chat = SimpleNamespace(id=chat_id)
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    return message


def _callback(*, chat_id: int = 100, user_id: int = 200) -> MagicMock:
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = MagicMock()
    callback.message.chat = SimpleNamespace(id=chat_id)
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


class CustomerBatchHelpersTests(unittest.TestCase):
    def test_keyboard_before_and_after_four_files(self) -> None:
        before = _batch_keyboard(3)
        after = _batch_keyboard(4)
        before_data = [
            button.callback_data
            for row in before.inline_keyboard
            for button in row
        ]
        after_data = [
            button.callback_data
            for row in after.inline_keyboard
            for button in row
        ]
        self.assertEqual(before_data, ["customer_batch:cancel"])
        self.assertEqual(
            after_data,
            ["customer_batch:process", "customer_batch:cancel"],
        )

    def test_accepted_message_formats(self) -> None:
        self.assertIn("1 из 4", _format_file_accepted_message(1))
        self.assertIn("Теперь можно запустить обработку.", _format_file_accepted_message(4))

    def test_add_customer_command_is_on_batch_router_not_legacy(self) -> None:
        batch_commands = []
        for observer in batch_module.router.message.handlers:
            for filter_obj in observer.filters:
                callback = getattr(filter_obj, "callback", None)
                if isinstance(callback, Command) and "add_customer" in callback.commands:
                    batch_commands.append(callback.commands)

        legacy_commands = []
        for observer in customer_add_module.router.message.handlers:
            for filter_obj in observer.filters:
                callback = getattr(filter_obj, "callback", None)
                if isinstance(callback, Command):
                    legacy_commands.extend(callback.commands)

        self.assertTrue(any("add_customer" in commands for commands in batch_commands))
        self.assertIn("add_customer_legacy", legacy_commands)
        self.assertNotIn("add_customer", legacy_commands)


class CustomerFileDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_photo(self) -> None:
        message = _photo_message(message_id=42, file_size=5)
        bot = _mock_bot(b"photo-bytes")
        downloaded = await download_customer_file_from_telegram(
            message,
            bot,
            max_file_bytes=1024,
        )
        self.assertEqual(downloaded.original_filename, "photo_42.jpg")
        self.assertEqual(downloaded.content, b"photo-bytes")
        self.assertEqual(downloaded.file_extension, "jpg")
        self.assertEqual(downloaded.telegram_file_id, "photo-42")

    async def test_download_pdf(self) -> None:
        message = _document_message(message_id=7, filename="passport.pdf")
        bot = _mock_bot(b"%PDF-1.4")
        downloaded = await download_customer_file_from_telegram(
            message,
            bot,
            max_file_bytes=1024,
        )
        self.assertEqual(downloaded.original_filename, "passport.pdf")
        self.assertEqual(downloaded.mime_type, "application/pdf")
        self.assertEqual(downloaded.content, b"%PDF-1.4")

    async def test_download_rejects_oversized_declared_size(self) -> None:
        message = _document_message(message_id=8, file_size=2048)
        bot = _mock_bot(b"x")
        with self.assertRaises(CustomerFileDownloadError):
            await download_customer_file_from_telegram(
                message,
                bot,
                max_file_bytes=1024,
            )
        bot.get_file.assert_not_called()


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerBatchUploadFlowTests(unittest.IsolatedAsyncioTestCase):
    _pg = None
    database_url: str

    @classmethod
    def setUpClass(cls) -> None:
        resolved = resolve_test_database_url()
        if resolved is None:
            raise unittest.SkipTest("PostgreSQL test backend unavailable")
        if resolved == "__testing_postgresql__":
            import testing.postgresql

            ensure_postgres_on_path()
            cls._pg = testing.postgresql.Postgresql()
            cls.database_url = cls._pg.url()
        else:
            cls.database_url = resolved

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._pg is not None:
            cls._pg.stop()
            cls._pg = None

    async def asyncSetUp(self) -> None:
        self.db = Database(self.database_url)
        await self.db.connect()
        self.repo = CustomerUploadBatchRepository(self.db.pool)
        self.settings = _settings(database_url=self.database_url)

    async def asyncTearDown(self) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE customer_upload_batches CASCADE;")
        await self.db.close()

    async def test_add_customer_creates_new_batch(self) -> None:
        message = _photo_message(message_id=1)
        state = _mock_state()
        await handle_add_customer_batch(message, state, self.repo)
        batches = await self.repo.get_active_batch(100, 200)
        self.assertIsNotNone(batches)
        self.assertEqual(batches["status"], CustomerUploadBatchStatus.COLLECTING)
        self.assertEqual(state._data["batch_id"], batches["id"])
        message.answer.assert_awaited()
        text = message.answer.await_args.args[0]
        self.assertIn("Добавление клиента", text)

    async def test_repeated_add_customer_reuses_collecting_batch(self) -> None:
        message = _photo_message(message_id=1)
        state = _mock_state()
        await handle_add_customer_batch(message, state, self.repo)
        first_id = state._data["batch_id"]
        await handle_add_customer_batch(message, state, self.repo)
        self.assertEqual(state._data["batch_id"], first_id)
        async with self.db.pool.acquire() as connection:
            count = await connection.fetchval(
                """
                SELECT COUNT(*)::bigint
                FROM customer_upload_batches
                WHERE telegram_chat_id = 100 AND telegram_user_id = 200;
                """
            )
        self.assertEqual(count, 1)

    async def test_photo_and_pdf_saved_as_bytea(self) -> None:
        message = _photo_message(message_id=1)
        state = _mock_state()
        await handle_add_customer_batch(message, state, self.repo)

        photo = _photo_message(message_id=11)
        await handle_batch_document_in_state(
            photo,
            state,
            _mock_bot(b"JPEGDATA"),
            self.settings,
            self.repo,
        )
        pdf = _document_message(message_id=12, filename="tin.pdf")
        await handle_batch_document_in_state(
            pdf,
            state,
            _mock_bot(b"%PDFDATA"),
            self.settings,
            self.repo,
        )

        files = await self.repo.get_batch_files(state._data["batch_id"])
        self.assertEqual(len(files), 2)
        contents = {row["temporary_content"] for row in files}
        self.assertEqual(contents, {b"JPEGDATA", b"%PDFDATA"})

    async def test_four_separate_messages_share_one_batch(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        batch_id = state._data["batch_id"]
        for message_id in (21, 22, 23, 24):
            await handle_batch_document_in_state(
                _photo_message(message_id=message_id),
                state,
                _mock_bot(f"img-{message_id}".encode()),
                self.settings,
                self.repo,
            )
        self.assertEqual(await self.repo.count_batch_files(batch_id), 4)
        files = await self.repo.get_batch_files(batch_id)
        self.assertEqual({row["batch_id"] for row in files}, {batch_id})

    async def test_media_group_shares_one_batch(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        batch_id = state._data["batch_id"]
        for message_id in (31, 32, 33, 34):
            await handle_batch_document_in_state(
                _photo_message(message_id=message_id, media_group_id="album-9"),
                state,
                _mock_bot(f"g-{message_id}".encode()),
                self.settings,
                self.repo,
            )
        batch = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(batch["media_group_id"], "album-9")
        self.assertEqual(await self.repo.count_batch_files(batch_id), 4)

    async def test_duplicate_update_does_not_create_second_file(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        message = _photo_message(message_id=41)
        bot = _mock_bot(b"same")
        await handle_batch_document_in_state(message, state, bot, self.settings, self.repo)
        message.answer.reset_mock()
        await handle_batch_document_in_state(message, state, bot, self.settings, self.repo)
        self.assertEqual(await self.repo.count_batch_files(state._data["batch_id"]), 1)
        message.answer.assert_not_awaited()

    async def test_progress_text_and_process_keyboard(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        first = _photo_message(message_id=51)
        await handle_batch_document_in_state(
            first,
            state,
            _mock_bot(b"1"),
            self.settings,
            self.repo,
        )
        self.assertIn("1 из 4", first.answer.await_args.args[0])
        keyboard = first.answer.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["customer_batch:cancel"])

        for message_id in (52, 53, 54):
            msg = _photo_message(message_id=message_id)
            await handle_batch_document_in_state(
                msg,
                state,
                _mock_bot(str(message_id).encode()),
                self.settings,
                self.repo,
            )
        fourth_keyboard = msg.answer.await_args.kwargs["reply_markup"]
        fourth_callbacks = [
            button.callback_data
            for row in fourth_keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("customer_batch:process", fourth_callbacks)
        self.assertIn("Теперь можно запустить обработку.", msg.answer.await_args.args[0])

    async def test_process_rejects_when_fewer_than_four(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        for message_id in (61, 62, 63):
            await handle_batch_document_in_state(
                _photo_message(message_id=message_id),
                state,
                _mock_bot(b"x"),
                self.settings,
                self.repo,
            )
        callback = _callback()
        recognition_service = AsyncMock()
        folder_service = AsyncMock()
        await handle_batch_process(
            callback,
            state,
            AsyncMock(),
            self.settings,
            self.repo,
            recognition_service,
            folder_service,
        )
        self.assertIn("Загружено только 3", callback.message.answer.await_args.args[0])
        batch = await self.repo.get_batch_by_id(state._data["batch_id"])
        self.assertEqual(batch["status"], CustomerUploadBatchStatus.COLLECTING)
        recognition_service.process_batch.assert_not_awaited()
        folder_service.ensure_batch_files_saved.assert_not_awaited()

    async def test_process_claims_batch_once(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        for message_id in (71, 72, 73, 74):
            await handle_batch_document_in_state(
                _photo_message(message_id=message_id),
                state,
                _mock_bot(b"x"),
                self.settings,
                self.repo,
            )
        callback = _callback()
        recognition_service = CustomerBatchRecognitionService(
            repository=self.repo,
            recognition_service=AsyncMock(
                recognize_document=AsyncMock(
                    side_effect=[
                        CustomerDocumentRecognitionResult(
                            document_type="passport_main",
                            confidence=0.9,
                            ocr_text="x",
                            extracted_fields={
                                "last_name": "Ivanov",
                                "first_name": "Ivan",
                                "passport": "1",
                            },
                            warnings=[],
                        ),
                        CustomerDocumentRecognitionResult(
                            document_type="passport_registration",
                            confidence=0.9,
                            ocr_text="x",
                            extracted_fields={},
                            warnings=[],
                        ),
                        CustomerDocumentRecognitionResult(
                            document_type="snils",
                            confidence=0.9,
                            ocr_text="x",
                            extracted_fields={
                                "last_name": "Ivanov",
                                "first_name": "Ivan",
                            },
                            warnings=[],
                        ),
                        CustomerDocumentRecognitionResult(
                            document_type="tin",
                            confidence=0.9,
                            ocr_text="x",
                            extracted_fields={
                                "last_name": "Ivanov",
                                "first_name": "Ivan",
                            },
                            warnings=[],
                        ),
                    ]
                )
            ),
        )
        folder_service = AsyncMock()
        folder_service.ensure_batch_files_saved = AsyncMock(
            return_value=FolderFinalizeResult(
                batch_id=state._data["batch_id"],
                customer_path="/base/02_Клиенты/Ivanov_1",
                uploaded_count=4,
                skipped_count=0,
                failed_count=0,
                status=CustomerUploadBatchStatus.FILES_SAVED,
            )
        )
        await handle_batch_process(
            callback,
            state,
            AsyncMock(),
            self.settings,
            self.repo,
            recognition_service,
            folder_service,
        )
        batch = await self.repo.get_batch_by_id(state._data["batch_id"])
        self.assertEqual(batch["status"], CustomerUploadBatchStatus.RECOGNIZED)
        texts = [call.args[0] for call in callback.message.answer.await_args_list]
        self.assertTrue(any("Распознаю документы" in text for text in texts))
        self.assertTrue(any("Распознанные данные клиента" in text for text in texts))
        folder_service.ensure_batch_files_saved.assert_awaited()

        callback.message.answer.reset_mock()
        await handle_batch_process(
            callback,
            state,
            AsyncMock(),
            self.settings,
            self.repo,
            recognition_service,
            folder_service,
        )
        # After recognition, second process resumes finalize/preview (no OCR for recognized).
        # Status is still recognized until folder service updates it in real flow.
        self.assertTrue(callback.message.answer.await_count >= 1)

    async def test_cancel_marks_abandoned_and_keeps_files(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        await handle_batch_document_in_state(
            _photo_message(message_id=81),
            state,
            _mock_bot(b"keep-me"),
            self.settings,
            self.repo,
        )
        batch_id = state._data["batch_id"]
        callback = _callback()
        await handle_batch_cancel(callback, state, self.repo)
        batch = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(batch["status"], CustomerUploadBatchStatus.ABANDONED)
        files = await self.repo.get_batch_files(batch_id)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["temporary_content"], b"keep-me")
        self.assertIsNone(await state.get_state())

    async def test_fsm_restore_from_active_batch(self) -> None:
        created = await self.repo.create_batch(
            batch_key=f"restore-{uuid.uuid4().hex}",
            telegram_chat_id=100,
            telegram_user_id=200,
        )
        empty_state = _mock_state()
        message = _photo_message(message_id=91)
        await handle_batch_document_restore(
            message,
            empty_state,
            _mock_bot(b"restored"),
            self.settings,
            self.repo,
        )
        self.assertEqual(empty_state._data["batch_id"], created["id"])
        self.assertEqual(await self.repo.count_batch_files(created["id"]), 1)
        self.assertEqual(
            empty_state._data.get("batch_id"),
            created["id"],
        )

    async def test_oversized_file_is_not_added(self) -> None:
        state = _mock_state()
        await handle_add_customer_batch(_photo_message(message_id=1), state, self.repo)
        message = _document_message(message_id=101, file_size=2048)
        await handle_batch_document_in_state(
            message,
            state,
            _mock_bot(b"too-big"),
            self.settings,
            self.repo,
        )
        self.assertEqual(await self.repo.count_batch_files(state._data["batch_id"]), 0)
        self.assertIn("слишком большой", message.answer.await_args.args[0])

    async def test_legacy_stepwise_handler_does_not_share_add_customer_command(self) -> None:
        batch_has_add_customer = False
        for observer in batch_module.router.message.handlers:
            for filter_obj in observer.filters:
                callback = getattr(filter_obj, "callback", None)
                if isinstance(callback, Command) and "add_customer" in callback.commands:
                    batch_has_add_customer = True

        legacy_commands: list[str] = []
        legacy_uses_stepwise_states = False
        for observer in customer_add_module.router.message.handlers:
            for filter_obj in observer.filters:
                callback = getattr(filter_obj, "callback", None)
                if isinstance(callback, Command):
                    legacy_commands.extend(callback.commands)
                filter_text = str(getattr(filter_obj, "callback", filter_obj))
                if CustomerAddStates.waiting_passport_main.state in filter_text:
                    legacy_uses_stepwise_states = True

        self.assertTrue(batch_has_add_customer)
        self.assertIn("add_customer_legacy", legacy_commands)
        self.assertNotIn("add_customer", legacy_commands)
        self.assertTrue(legacy_uses_stepwise_states)
        self.assertNotEqual(
            CustomerAddStates.waiting_passport_main.state,
            CustomerBatchUploadStates.collecting_documents.state,
        )

        # Restore path must not steal unrelated chats without an active batch.
        other_message = _photo_message(message_id=112, chat_id=999, user_id=999)
        with self.assertRaises(SkipHandler):
            await handle_batch_document_restore(
                other_message,
                _mock_state(),
                _mock_bot(b"x"),
                self.settings,
                self.repo,
            )


if __name__ == "__main__":
    unittest.main()
