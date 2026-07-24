from __future__ import annotations

import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.database import Database
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_verification_service import (
    CustomerBatchVerificationService,
    build_save_warnings,
)
from app.web.app import create_fastapi_app
from app.web.auth import build_telegram_init_data_for_tests
from app.web.token_service import create_customer_batch_context_token
from app.yadisk_client import ConflictYandexDiskError
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


BOT_TOKEN = "123456:ABC-DEF"
SECRET = "mini-app-secret"
NOW = 1_700_000_000
USER_ID = 20
CHAT_ID = 10


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": BOT_TOKEN,
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
        "mini_app_token_secret": SECRET,
        "web_host": "0.0.0.0",
        "web_port": 8000,
        "mini_app_token_ttl_seconds": 900,
        "telegram_init_data_max_age_seconds": 900,
        "customer_upload_max_file_bytes": 1024,
    }
    values.update(overrides)
    return Settings(**values)


def _batch_token(*, batch_id: int, telegram_user_id: int = USER_ID) -> str:
    return create_customer_batch_context_token(
        secret=SECRET,
        batch_id=batch_id,
        telegram_user_id=telegram_user_id,
        origin_chat_id=CHAT_ID,
        ttl_seconds=900,
        now=NOW,
    )


def _init_data(*, telegram_user_id: int = USER_ID) -> str:
    return build_telegram_init_data_for_tests(
        bot_token=BOT_TOKEN,
        user={"id": telegram_user_id, "first_name": "Ivan"},
        auth_date=NOW - 5,
    )


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerManualVerificationTests(unittest.IsolatedAsyncioTestCase):
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
        self.disk.path_exists = AsyncMock(return_value=False)
        self.disk.move_resource = AsyncMock(
            side_effect=lambda src, dst, overwrite=False: dst
        )
        self.service = CustomerBatchVerificationService(
            repository=self.repo,
            yandex_disk_client=self.disk,
        )
        self.bot = AsyncMock()
        self.settings = _settings(database_url=self.database_url)
        self.app = create_fastapi_app(
            settings=self.settings,
            database=self.db,
            bot=self.bot,
            yandex_disk_client=self.disk,
        )
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE customer_upload_batches CASCADE;")
            await connection.execute("TRUNCATE customers CASCADE;")
        await self.db.close()

    async def _seed_batch(self, *, with_unknown: bool = False) -> dict:
        batch = await self.repo.create_batch(
            batch_key=f"verify-{uuid.uuid4().hex}",
            telegram_chat_id=CHAT_ID,
            telegram_user_id=USER_ID,
        )
        await self.repo.claim_batch_for_processing(batch["id"])
        specs = [
            ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "80 06 035956"}),
            (
                "passport_registration" if not with_unknown else "unknown",
                {"registration_address": "г. Уфа"} if not with_unknown else {},
            ),
            ("snils", {"last_name": "Ivanov", "first_name": "Ivan", "ipain": "123-456-789 00"}),
            ("tin", {"last_name": "Petrov", "first_name": "Petr", "tin": "123456789012"}),
        ]
        folder = "/auto_export_demo/02_Клиенты/Ivanov_8006035956"
        await self.repo.set_batch_customer_path(batch["id"], folder)
        names = {
            "passport_main": "Паспорт.jpg",
            "passport_registration": "Прописка.jpg",
            "unknown": "Неопознанный_1.jpg",
            "snils": "Снилс.png",
            "tin": "Инн.jpg",
        }
        for index, (doc_type, fields) in enumerate(specs, start=1):
            row = await self.repo.add_file(
                batch_id=batch["id"],
                telegram_message_id=index,
                temporary_content=b"x",
                original_filename=f"{doc_type}.jpg",
                mime_type="image/jpeg",
                file_extension="jpg",
            )
            await self.repo.mark_file_recognized(
                row["id"],
                detected_document_type=doc_type,
                extracted_json={"document_type": doc_type, "fields": fields},
            )
            await self.repo.set_file_yadisk_path(
                row["id"],
                f"{folder}/{names[doc_type]}",
            )
            await self.repo.clear_file_temporary_content(row["id"])
        await self.repo.set_batch_recognized(batch["id"])
        await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.FILES_SAVED,
        )
        return await self.repo.get_batch_by_id(batch["id"])

    async def test_form_save_creates_customer_and_links_batch(self) -> None:
        batch = await self._seed_batch()
        token = _batch_token(batch_id=batch["id"])
        payload = {
            "passport": "80 06 035956",
            "last_name": "Ivanov",
            "first_name": "Ivan",
            "surname": "Ivanovich",
            "registration_address": "г. Уфа",
            "ipain": "123-456-789 00",
            "tin": "123456789012",
            "context_token": token,
            "telegram_init_data": _init_data(),
        }
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.put("/api/customers", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "Клиент сохранён")
        customer_id = body["customer_id"]
        customer = await self.db.get_customer_by_id(customer_id)
        self.assertEqual(
            customer["customer_path"],
            "/auto_export_demo/02_Клиенты/Ivanov_8006035956",
        )
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.CUSTOMER_SAVED)
        self.assertIsNotNone(refreshed["completed_at"])
        self.assertEqual(int(refreshed["customer_id"]), int(customer_id))
        self.bot.send_message.assert_awaited()
        notify_kwargs = self.bot.send_message.await_args.kwargs
        notify_text = notify_kwargs.get("text")
        if notify_text is None and self.bot.send_message.await_args.args:
            notify_text = self.bot.send_message.await_args.args[0]
            if len(self.bot.send_message.await_args.args) > 1:
                notify_text = self.bot.send_message.await_args.args[1]
        self.assertIn("Клиент сохранён", notify_text)
        self.assertIn("/auto_export_demo/02_Клиенты/Ivanov_8006035956", notify_text)

    async def test_repeat_submit_is_idempotent(self) -> None:
        batch = await self._seed_batch()
        token = _batch_token(batch_id=batch["id"])
        payload = {
            "passport": "80 06 035956",
            "last_name": "Ivanov",
            "first_name": "Ivan",
            "context_token": token,
            "telegram_init_data": _init_data(),
        }
        with patch("app.web.auth.time.time", return_value=NOW):
            first = await self.client.put("/api/customers", json=payload)
            second = await self.client.put("/api/customers", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["customer_id"], second.json()["customer_id"])
        self.assertTrue(second.json().get("already_saved"))
        self.assertEqual(second.json()["message"], "Клиент уже сохранён")
        async with self.db.pool.acquire() as connection:
            count = await connection.fetchval("SELECT COUNT(*) FROM customers;")
        self.assertEqual(int(count), 1)

    async def test_assign_unknown_as_registration_and_rename(self) -> None:
        batch = await self._seed_batch(with_unknown=True)
        files = await self.repo.get_batch_files_with_paths(batch["id"])
        unknown = next(
            row for row in files if row["detected_document_type"] == "unknown"
        )
        result = await self.service.update_file_document_type(
            batch_id=batch["id"],
            file_id=int(unknown["id"]),
            document_type="passport_registration",
        )
        self.assertEqual(
            result.file["detected_document_type"],
            "passport_registration",
        )
        self.assertTrue(result.renamed)
        self.disk.move_resource.assert_awaited()
        moved_to = self.disk.move_resource.await_args.args[1]
        self.assertTrue(moved_to.endswith("/Прописка.jpg"))
        refreshed = await self.repo.get_batch_file(int(unknown["id"]))
        self.assertEqual(refreshed["final_yadisk_path"], moved_to)
        self.assertTrue(result.kit["is_complete"])
        self.assertTrue(any("назначена вручную" in item for item in result.warnings))

    async def test_rename_conflict_uses_suffix(self) -> None:
        batch = await self._seed_batch(with_unknown=True)
        files = await self.repo.get_batch_files_with_paths(batch["id"])
        unknown = next(
            row for row in files if row["detected_document_type"] == "unknown"
        )

        async def path_exists(path: str) -> bool:
            return path.endswith("/Прописка.jpg")

        self.disk.path_exists = AsyncMock(side_effect=path_exists)
        result = await self.service.update_file_document_type(
            batch_id=batch["id"],
            file_id=int(unknown["id"]),
            document_type="passport_registration",
        )
        moved_to = self.disk.move_resource.await_args.args[1]
        self.assertTrue(moved_to.endswith("/Прописка_1.jpg"))
        self.assertEqual(result.file["final_yadisk_path"], moved_to)

    async def test_move_failure_keeps_source_and_type(self) -> None:
        batch = await self._seed_batch(with_unknown=True)
        files = await self.repo.get_batch_files_with_paths(batch["id"])
        unknown = next(
            row for row in files if row["detected_document_type"] == "unknown"
        )
        source = unknown["final_yadisk_path"]
        self.disk.move_resource = AsyncMock(side_effect=RuntimeError("disk locked"))
        result = await self.service.update_file_document_type(
            batch_id=batch["id"],
            file_id=int(unknown["id"]),
            document_type="passport_registration",
        )
        self.assertIsNotNone(result.move_error)
        refreshed = await self.repo.get_batch_file(int(unknown["id"]))
        self.assertEqual(
            refreshed["detected_document_type"],
            "passport_registration",
        )
        self.assertEqual(refreshed["final_yadisk_path"], source)

    async def test_fio_warning_does_not_block_save(self) -> None:
        batch = await self._seed_batch()
        warnings = build_save_warnings(
            fields={
                "last_name": "Ivanov",
                "first_name": "Ivan",
                "passport": "80 06 035956",
                "registration_address": "г. Уфа",
            },
            kit_warnings=["ФИО паспорта не совпадает с ФИО в ИНН"],
        )
        self.assertTrue(any("ФИО" in item for item in warnings))
        token = _batch_token(batch_id=batch["id"])
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.put(
                "/api/customers",
                json={
                    "passport": "80 06 035956",
                    "last_name": "Ivanov",
                    "first_name": "Ivan",
                    "tin": "123456789012",
                    "context_token": token,
                    "telegram_init_data": _init_data(),
                },
            )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_missing_address_warning_allows_save(self) -> None:
        batch = await self._seed_batch()
        token = _batch_token(batch_id=batch["id"])
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.put(
                "/api/customers",
                json={
                    "passport": "80 06 035956",
                    "last_name": "Ivanov",
                    "first_name": "Ivan",
                    "registration_address": "",
                    "context_token": token,
                    "telegram_init_data": _init_data(),
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(
            any("Адрес регистрации" in item for item in body.get("warnings", []))
        )

    async def test_document_type_forbidden_after_customer_saved(self) -> None:
        batch = await self._seed_batch()
        files = await self.repo.get_batch_files_with_paths(batch["id"])
        customer = await self.db.create_customer(
            {
                "passport": f"unique-{uuid.uuid4().hex[:8]}",
                "first_name": "Ivan",
                "last_name": "Ivanov",
                "customer_path": batch["customer_path"],
            }
        )
        await self.repo.mark_batch_customer_saved(
            batch["id"],
            customer_id=int(customer["id"]),
        )
        token = _batch_token(batch_id=batch["id"])
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.patch(
                f"/api/customer-batches/{batch['id']}/files/{files[0]['id']}/document-type",
                json={
                    "document_type": "snils",
                    "context_token": token,
                    "telegram_init_data": _init_data(),
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "BATCH_ALREADY_SAVED")

    async def test_token_cannot_change_foreign_batch(self) -> None:
        batch = await self._seed_batch()
        other = await self._seed_batch()
        files = await self.repo.get_batch_files_with_paths(other["id"])
        token = _batch_token(batch_id=batch["id"])
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.patch(
                f"/api/customer-batches/{other['id']}/files/{files[0]['id']}/document-type",
                json={
                    "document_type": "tin",
                    "context_token": token,
                    "telegram_init_data": _init_data(),
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "BATCH_FORBIDDEN")

    async def test_edit_context_returns_documents_and_kit(self) -> None:
        batch = await self._seed_batch()
        token = _batch_token(batch_id=batch["id"])
        with patch("app.web.auth.time.time", return_value=NOW):
            response = await self.client.post(
                "/api/customers/edit-context",
                json={
                    "context_token": token,
                    "telegram_init_data": _init_data(),
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["documents"]), 4)
        self.assertIn("summary", body["kit"])
        self.assertFalse(body["already_saved"])

    async def test_customer_id_column_exists(self) -> None:
        async with self.db.pool.acquire() as connection:
            exists = await connection.fetchval(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'customer_upload_batches'
                  AND column_name = 'customer_id';
                """
            )
        self.assertTrue(exists)


class MoveResourceConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_error_is_raised_for_409(self) -> None:
        from app.yadisk_client import YandexDiskClient

        client = YandexDiskClient(token="t", base_path="/base")

        class FakeResponse:
            status = 409

            async def text(self):
                return "exists"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, url):
                return FakeResponse()

        with patch("app.yadisk_client.aiohttp.ClientSession", FakeSession):
            with patch.object(
                YandexDiskClient,
                "_ensure_parent_directory",
                new=AsyncMock(),
            ):
                with self.assertRaises(ConflictYandexDiskError):
                    await client.move_resource(
                        "/base/a.jpg",
                        "/base/b.jpg",
                        overwrite=False,
                    )


if __name__ == "__main__":
    unittest.main()
