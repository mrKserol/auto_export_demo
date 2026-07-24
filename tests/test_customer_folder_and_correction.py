from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.database import Database
from app.handlers.customer_batch_upload import (
    handle_batch_edit,
    _post_recognition_keyboard,
)
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchFileRecognitionStatus,
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_data_service import (
    assemble_customer_data_from_batch_files,
    customer_fields_for_create,
    format_customer_data_preview,
)
from app.services.customer_folder_service import (
    CustomerFolderService,
    assign_disk_filenames,
)
from app.web.token_service import (
    PURPOSE_CREATE_CUSTOMER_FROM_BATCH,
    create_customer_batch_context_token,
    verify_customer_batch_context_token,
)
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "1:TEST",
        "database_url": "postgresql://example",
        "yandex_disk_token": "token",
        "yandex_disk_base_path": "/auto_export_demo",
        "yandex_function_url": None,
        "enable_processing": False,
        "yandex_api_key": "key",
        "yandex_cloud_folder_id": "folder",
        "ocr_min_delay_seconds": 1.5,
        "max_ocr_retries": 3,
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


class CustomerFolderNamingTests(unittest.TestCase):
    def test_folder_name_from_last_name_and_passport(self) -> None:
        name = CustomerFolderService.build_customer_folder_name(
            "Губайдуллина",
            "80 11 541410",
        )
        self.assertEqual(name, "Губайдуллина_8011541410")

    def test_passport_sanitized(self) -> None:
        name = CustomerFolderService.build_customer_folder_name(
            "Ivanov",
            "80-06 035956",
        )
        self.assertEqual(name, "Ivanov_8006035956")

    def test_assign_filenames_keep_extension_and_duplicate_suffix(self) -> None:
        files = [
            {
                "id": 1,
                "detected_document_type": "passport_main",
                "file_extension": "jpg",
                "original_filename": "a.jpg",
            },
            {
                "id": 2,
                "detected_document_type": "passport_main",
                "file_extension": "jpg",
                "original_filename": "b.jpg",
            },
            {
                "id": 3,
                "detected_document_type": "unknown",
                "file_extension": "pdf",
                "original_filename": "x.pdf",
            },
            {
                "id": 4,
                "detected_document_type": "snils",
                "file_extension": "png",
                "original_filename": "s.png",
            },
        ]
        names = assign_disk_filenames(files)
        self.assertEqual(names[1], "Паспорт_1.jpg")
        self.assertEqual(names[2], "Паспорт_2.jpg")
        self.assertEqual(names[3], "Неопознанный_1.pdf")
        self.assertEqual(names[4], "Снилс.png")


class CustomerBatchDataServiceTests(unittest.TestCase):
    def test_assemble_from_four_documents_and_empty_address(self) -> None:
        files = [
            {
                "id": 1,
                "detected_document_type": "passport_main",
                "extracted_json": {
                    "fields": {
                        "last_name": "Ivanov",
                        "first_name": "Ivan",
                        "surname": "Ivanovich",
                        "passport": "80 06 035956",
                        "date_issue": "01.01.2020",
                        "department_code": "020-001",
                    }
                },
            },
            {
                "id": 2,
                "detected_document_type": "unknown",
                "extracted_json": {"document_type": "unknown"},
            },
            {
                "id": 3,
                "detected_document_type": "snils",
                "extracted_json": {
                    "fields": {
                        "last_name": "Ivanov",
                        "first_name": "Ivan",
                        "ipain": "123-456-789 00",
                    }
                },
            },
            {
                "id": 4,
                "detected_document_type": "tin",
                "extracted_json": {
                    "fields": {
                        "last_name": "Petrov",
                        "first_name": "Petr",
                        "tin": "123456789012",
                    }
                },
            },
        ]
        assembled = assemble_customer_data_from_batch_files(files)
        self.assertEqual(assembled.fields["last_name"], "Ivanov")
        self.assertEqual(assembled.fields["passport"], "80 06 035956")
        self.assertEqual(assembled.fields["ipain"], "123-456-789 00")
        self.assertEqual(assembled.fields["tin"], "123456789012")
        self.assertIsNone(assembled.fields["registration_address"])
        self.assertTrue(any("ИНН" in warning for warning in assembled.warnings))
        preview = format_customer_data_preview(assembled)
        self.assertIn("Адрес регистрации: не распознано", preview)
        payload = customer_fields_for_create(
            assembled,
            customer_path="/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        self.assertEqual(
            payload["customer_path"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        self.assertIn("last_name_translit", payload)

    def test_manual_correction_button_present(self) -> None:
        keyboard = _post_recognition_keyboard(include_edit=True)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertEqual(
            callbacks,
            [
                "customer_batch:edit",
                "customer_batch:retry_recognition",
                "customer_batch:cancel",
            ],
        )

    def test_batch_token_has_no_personal_data(self) -> None:
        token = create_customer_batch_context_token(
            secret="secret",
            batch_id=42,
            telegram_user_id=7,
            origin_chat_id=-100,
            ttl_seconds=900,
            now=1_700_000_000,
        )
        self.assertNotIn("Ivanov", token)
        self.assertNotIn("passport", token.lower())
        context = verify_customer_batch_context_token(
            token,
            secret="secret",
            expected_telegram_user_id=7,
            now=1_700_000_000,
        )
        self.assertEqual(context.purpose, PURPOSE_CREATE_CUSTOMER_FROM_BATCH)
        self.assertEqual(context.batch_id, 42)


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerFolderServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.service = CustomerFolderService(
            repository=self.repo,
            yandex_disk_client=self.disk,
        )

    async def asyncTearDown(self) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE customer_upload_batches CASCADE;")
            await connection.execute("TRUNCATE customers CASCADE;")
        await self.db.close()

    async def _seed_batch(self) -> dict:
        batch = await self.repo.create_batch(
            batch_key=f"folder-{uuid.uuid4().hex}",
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
            fields = {"last_name": "Ivanov", "first_name": "Ivan", "passport": "80 06 035956"}
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

    async def test_create_unique_folder_with_suffix_on_conflict(self) -> None:
        self.disk.try_create_directory = AsyncMock(side_effect=[False, True])
        path = await self.service.create_unique_customer_folder(
            last_name="Ivanov",
            passport="80 06 035956",
        )
        self.assertEqual(path, "/auto_export_demo/02_Клиенты/Ivanov_8006035956_1")

    async def test_ensure_batch_files_saved_uploads_and_clears_content(self) -> None:
        batch = await self._seed_batch()
        self.disk.try_create_directory = AsyncMock(return_value=True)
        self.disk.path_exists = AsyncMock(return_value=False)
        self.disk.upload_bytes = AsyncMock(
            side_effect=lambda path, content, overwrite=False: path
        )
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(result.status, CustomerUploadBatchStatus.FILES_SAVED)
        self.assertEqual(result.uploaded_count, 4)
        self.assertEqual(result.failed_count, 0)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(
            refreshed["customer_path"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        files = await self.repo.get_batch_files(batch["id"])
        self.assertTrue(all(row["final_yadisk_path"] for row in files))
        self.assertTrue(all(row["temporary_content"] is None for row in files))

    async def test_retry_uses_existing_customer_path_and_skips_uploaded(self) -> None:
        batch = await self._seed_batch()
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

        self.disk.ensure_directory = AsyncMock()
        self.disk.try_create_directory = AsyncMock()
        self.disk.path_exists = AsyncMock(return_value=False)
        self.disk.upload_bytes = AsyncMock(
            side_effect=lambda path, content, overwrite=False: path
        )
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.disk.try_create_directory.assert_not_awaited()
        self.disk.ensure_directory.assert_awaited()
        self.assertEqual(result.uploaded_count, 3)
        self.assertEqual(result.skipped_count, 1)

    async def test_partial_upload_failure_keeps_folder_and_content(self) -> None:
        batch = await self._seed_batch()
        self.disk.try_create_directory = AsyncMock(return_value=True)
        self.disk.path_exists = AsyncMock(return_value=False)

        async def upload(path, content, overwrite=False):
            if content == b"snils":
                raise RuntimeError("disk down")
            return path

        self.disk.upload_bytes = AsyncMock(side_effect=upload)
        result = await self.service.ensure_batch_files_saved(batch["id"])
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.RECOGNIZED)
        self.assertIsNotNone(refreshed["customer_path"])
        files = await self.repo.get_batch_files(batch["id"])
        by_content = {
            row["temporary_content"]: row
            for row in files
            if row["temporary_content"] is not None
        }
        self.assertIn(b"snils", by_content)

    async def test_customer_saved_with_customer_path(self) -> None:
        batch = await self._seed_batch()
        await self.repo.set_batch_customer_path(
            batch["id"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        files = await self.repo.get_batch_files(batch["id"])
        assembled = assemble_customer_data_from_batch_files(files)
        payload = customer_fields_for_create(
            assembled,
            customer_path="/auto_export_demo/02_Клиенты/Ivanov_8006035956",
            overrides={"phone": "+79990001122", "email": "a@b.c"},
        )
        customer = await self.db.create_customer(payload)
        await self.repo.mark_batch_customer_saved(batch["id"])
        self.assertEqual(
            customer["customer_path"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.CUSTOMER_SAVED)
        self.assertIsNotNone(refreshed["completed_at"])
        # FIO warning must not block create
        assembled.warnings.append("ФИО паспорта не совпадает с ФИО в СНИЛС")
        self.assertTrue(assembled.warnings)

    async def test_edit_callback_opens_tokenized_form(self) -> None:
        batch = await self._seed_batch()
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.FILES_SAVED,
        )
        callback = MagicMock()
        callback.from_user = SimpleNamespace(id=20)
        callback.message = MagicMock()
        callback.message.chat = SimpleNamespace(id=10, type="private")
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"batch_id": batch["id"]})
        state.update_data = AsyncMock()
        settings = _settings(database_url=self.database_url)
        await handle_batch_edit(
            callback, state, settings, self.repo, AsyncMock(), self.db
        )
        text = callback.message.answer.await_args.args[0]
        self.assertIn("Данные клиента", text)
        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        url = markup.inline_keyboard[0][0].web_app.url
        self.assertTrue(url.startswith("https://example.com/miniapp/customer?token="))
        self.assertNotIn("Ivanov", url)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(
            refreshed["status"],
            CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        )


if __name__ == "__main__":
    unittest.main()
