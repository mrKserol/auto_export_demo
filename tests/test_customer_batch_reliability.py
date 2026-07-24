from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import resolve_app_commit_sha
from app.database import Database
from app.handlers.customer_batch_upload import (
    handle_batch_cancel,
    handle_batch_edit,
    handle_batch_process,
)
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_folder_service import (
    ALREADY_SAVING_OR_SAVED,
    BATCH_ABANDONED,
    CustomerFolderService,
)
from app.yadisk_client import YandexDiskClient, _RetryableYandexDiskError
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


def _callback() -> MagicMock:
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=20)
    callback.message = MagicMock()
    callback.message.chat = SimpleNamespace(id=10, type="private")
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _mock_state(batch_id: int) -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"batch_id": batch_id})
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    return state


class ResolveAppCommitShaTests(unittest.TestCase):
    def test_prefers_railway_git_commit_sha(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAILWAY_GIT_COMMIT_SHA": "railwaysha123",
                "GIT_COMMIT_SHA": "gitsha456",
            },
            clear=False,
        ):
            self.assertEqual(resolve_app_commit_sha(), "railwaysha123")

    def test_falls_back_to_git_commit_sha(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "RAILWAY_GIT_COMMIT_SHA"}
        env["GIT_COMMIT_SHA"] = "gitsha456"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_app_commit_sha(), "gitsha456")

    def test_unknown_when_missing(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"}
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_app_commit_sha(), "unknown")


class YandexDiskRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_423_then_succeeds(self) -> None:
        client = YandexDiskClient(token="token", base_path="/base")
        statuses = [423, 423, 201]

        class FakeResponse:
            def __init__(self, status: int):
                self.status = status

            async def text(self) -> str:
                return "LOCKED"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def put(self, url):
                return FakeResponse(statuses.pop(0))

        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        with patch("app.yadisk_client.asyncio.sleep", side_effect=fake_sleep):
            created = await client._put_directory(FakeSession(), "/base/folder")
        self.assertTrue(created)
        self.assertEqual(sleeps, [1, 2])

    async def test_retries_exhausted_raises_diagnostic_error(self) -> None:
        client = YandexDiskClient(token="token", base_path="/base")

        class FakeResponse:
            status = 423

            async def text(self) -> str:
                return "LOCKED"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def put(self, url):
                return FakeResponse()

        with patch("app.yadisk_client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(_RetryableYandexDiskError) as ctx:
                await client._put_directory(FakeSession(), "/base/folder")
        self.assertEqual(ctx.exception.status, 423)


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerBatchReliabilityTests(unittest.IsolatedAsyncioTestCase):
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
        self.disk = AsyncMock()
        self.disk.base_path = "/auto_export_demo"
        self.disk.build_customer_folder_path = (
            lambda name: f"/auto_export_demo/02_Клиенты/{name}"
        )
        self.disk.build_customer_file_path = (
            lambda folder, name: f"/auto_export_demo/02_Клиенты/{folder}/{name}"
        )
        self.disk.try_create_directory = AsyncMock(return_value=True)
        self.disk.ensure_directory = AsyncMock()
        self.disk.path_exists = AsyncMock(return_value=False)
        self.disk.upload_bytes = AsyncMock(
            side_effect=lambda path, content, overwrite=False: path
        )
        self.service = CustomerFolderService(
            repository=self.repo,
            yandex_disk_client=self.disk,
        )

    async def asyncTearDown(self) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE customer_upload_batches CASCADE;")
            await connection.execute("TRUNCATE customers CASCADE;")
        await self.db.close()

    async def _seed_recognized_batch(self) -> dict:
        batch = await self.repo.create_batch(
            batch_key=f"reliability-{uuid.uuid4().hex}",
            telegram_chat_id=10,
            telegram_user_id=20,
        )
        await self.repo.claim_batch_for_processing(batch["id"])
        files_spec = [
            ("passport_main", b"pass", "jpg"),
            ("passport_registration", b"reg", "pdf"),
            ("snils", b"snils", "png"),
            ("tin", b"tin", "jpg"),
        ]
        for index, (doc_type, content, ext) in enumerate(files_spec, start=1):
            row = await self.repo.add_file(
                batch_id=batch["id"],
                telegram_message_id=index,
                temporary_content=content,
                original_filename=f"{doc_type}.{ext}",
                mime_type="image/jpeg" if ext != "pdf" else "application/pdf",
                file_extension=ext,
            )
            fields = {
                "last_name": "Ivanov",
                "first_name": "Ivan",
                "passport": "80 06 035956",
            }
            if doc_type == "snils":
                fields = {"last_name": "Ivanov", "first_name": "Ivan", "ipain": "1"}
            if doc_type == "tin":
                fields = {"last_name": "Ivanov", "first_name": "Ivan", "tin": "2"}
            if doc_type == "passport_registration":
                fields = {"registration_address": "г. Уфа"}
            await self.repo.mark_file_recognized(
                row["id"],
                detected_document_type=doc_type,
                extracted_json={"document_type": doc_type, "fields": fields},
            )
        await self.repo.set_batch_recognized(batch["id"])
        return await self.repo.get_batch_by_id(batch["id"])

    async def test_concurrent_claim_only_one_wins(self) -> None:
        batch = await self._seed_recognized_batch()
        results = await asyncio.gather(
            self.repo.claim_batch_for_file_saving(batch["id"]),
            self.repo.claim_batch_for_file_saving(batch["id"]),
            self.repo.claim_batch_for_file_saving(batch["id"]),
        )
        self.assertEqual(sorted(results), [False, False, True])
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(
            refreshed["status"],
            CustomerUploadBatchStatus.CREATING_FOLDER,
        )

    async def test_second_callback_does_not_start_upload(self) -> None:
        batch = await self._seed_recognized_batch()
        first = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(first.status, CustomerUploadBatchStatus.FILES_SAVED)
        upload_calls = self.disk.upload_bytes.await_count
        second = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(second.error_message, ALREADY_SAVING_OR_SAVED)
        self.assertEqual(self.disk.upload_bytes.await_count, upload_calls)

    async def test_final_yadisk_path_skips_reupload(self) -> None:
        batch = await self._seed_recognized_batch()
        await self.repo.set_batch_customer_path(
            batch["id"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        files = await self.repo.get_batch_files(batch["id"])
        await self.repo.set_file_yadisk_path(
            files[0]["id"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956/Паспорт.jpg",
        )
        await self.repo.clear_file_temporary_content(files[0]["id"])
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.uploaded_count, 3)
        self.assertEqual(self.disk.upload_bytes.await_count, 3)

    async def test_uploads_are_sequential(self) -> None:
        batch = await self._seed_recognized_batch()
        in_flight = 0
        max_in_flight = 0

        async def upload(path, content, overwrite=False):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return path

        self.disk.upload_bytes = AsyncMock(side_effect=upload)
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(result.uploaded_count, 4)
        self.assertEqual(max_in_flight, 1)

    async def test_cancel_rejected_while_uploading(self) -> None:
        batch = await self._seed_recognized_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.UPLOADING,
        )
        callback = _callback()
        state = _mock_state(batch["id"])
        await handle_batch_cancel(callback, state, self.repo)
        text = callback.message.answer.await_args.args[0]
        self.assertIn("обрабатываются", text)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.UPLOADING)

    async def test_abandoned_stops_orchestration(self) -> None:
        batch = await self._seed_recognized_batch()
        upload_started = asyncio.Event()

        async def upload(path, content, overwrite=False):
            upload_started.set()
            await self.repo.update_batch_status(
                batch["id"],
                CustomerUploadBatchStatus.ABANDONED,
            )
            await asyncio.sleep(0.01)
            return path

        self.disk.upload_bytes = AsyncMock(side_effect=upload)
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(result.error_message, BATCH_ABANDONED)
        self.assertEqual(result.status, CustomerUploadBatchStatus.ABANDONED)
        self.assertLess(self.disk.upload_bytes.await_count, 4)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.ABANDONED)
        self.assertNotEqual(
            refreshed["status"],
            CustomerUploadBatchStatus.FILES_SAVED,
        )

    async def test_files_saved_cannot_be_cancelled(self) -> None:
        batch = await self._seed_recognized_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.FILES_SAVED,
        )
        callback = _callback()
        state = _mock_state(batch["id"])
        await handle_batch_cancel(callback, state, self.repo)
        text = callback.message.answer.await_args.args[0]
        self.assertIn("уже сохранены", text)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.FILES_SAVED)

    async def test_form_not_opened_before_files_saved(self) -> None:
        batch = await self._seed_recognized_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.UPLOADING,
        )
        callback = _callback()
        state = _mock_state(batch["id"])
        settings = MagicMock()
        settings.mini_app_base_url = "https://example.com"
        settings.mini_app_token_secret = "secret"
        settings.mini_app_token_ttl_seconds = 900
        await handle_batch_edit(
            callback, state, settings, self.repo, AsyncMock(), self.db
        )
        text = callback.message.answer.await_args.args[0]
        self.assertIn("после сохранения", text)
        self.assertNotIn("reply_markup", callback.message.answer.await_args.kwargs)

    async def test_form_reopens_after_files_saved(self) -> None:
        from app.config import Settings

        batch = await self._seed_recognized_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.FILES_SAVED,
        )
        callback = _callback()
        state = _mock_state(batch["id"])
        settings = Settings(
            telegram_bot_token="1:TEST",
            database_url=self.database_url,
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
        bot = AsyncMock()
        await handle_batch_edit(
            callback, state, settings, self.repo, bot, self.db
        )
        first_url = callback.message.answer.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard[0][0].web_app.url
        callback.message.answer.reset_mock()
        await handle_batch_edit(
            callback, state, settings, self.repo, bot, self.db
        )
        second_url = callback.message.answer.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard[0][0].web_app.url
        self.assertTrue(first_url.startswith("https://example.com/miniapp/customer"))
        self.assertTrue(second_url.startswith("https://example.com/miniapp/customer"))

    async def test_process_rejects_busy_batch(self) -> None:
        batch = await self._seed_recognized_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.CREATING_FOLDER,
        )
        callback = _callback()
        state = _mock_state(batch["id"])
        folder_service = AsyncMock()
        await handle_batch_process(
            callback,
            state,
            AsyncMock(),
            MagicMock(),
            self.repo,
            AsyncMock(),
            folder_service,
        )
        self.assertIn(
            "уже сохраняются или сохранены",
            callback.message.answer.await_args.args[0],
        )
        folder_service.ensure_batch_files_saved.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
