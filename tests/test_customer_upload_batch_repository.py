from __future__ import annotations

import os
import shutil
import unittest
import uuid
from pathlib import Path

from app.database import Database
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)


def _postgres_bin_dirs() -> list[Path]:
    roots = [
        Path("/Library/PostgreSQL"),
        Path("/usr/local/pgsql"),
        Path("/opt/homebrew/opt"),
        Path("/usr/lib/postgresql"),
    ]
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.rglob("bin"):
            if (candidate / "postgres").exists() and (candidate / "initdb").exists():
                found.append(candidate)
    return found


def _ensure_postgres_on_path() -> bool:
    if shutil.which("postgres") and shutil.which("initdb"):
        return True
    for bin_dir in _postgres_bin_dirs():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        if shutil.which("postgres") and shutil.which("initdb"):
            return True
    return False


def _resolve_test_database_url() -> str | None:
    env_url = os.getenv("TEST_DATABASE_URL")
    if env_url:
        return env_url

    if not _ensure_postgres_on_path():
        return None

    try:
        import testing.postgresql
    except ImportError:
        return None

    return "__testing_postgresql__"


@unittest.skipUnless(
    _resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerUploadBatchRepositoryTests(unittest.IsolatedAsyncioTestCase):
    _pg = None
    database_url: str

    @classmethod
    def setUpClass(cls) -> None:
        resolved = _resolve_test_database_url()
        if resolved is None:
            raise unittest.SkipTest("PostgreSQL test backend unavailable")
        if resolved == "__testing_postgresql__":
            import testing.postgresql

            _ensure_postgres_on_path()
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

    async def asyncTearDown(self) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute(
                "TRUNCATE customer_upload_batches CASCADE;"
            )
            await connection.execute("TRUNCATE customers CASCADE;")
        await self.db.close()

    async def test_migrations_are_idempotent(self) -> None:
        second = Database(self.database_url)
        await second.connect()
        await second.close()

        async with self.db.pool.acquire() as connection:
            customers_path = await connection.fetchval(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'customers'
                  AND column_name = 'customer_path';
                """
            )
            batches = await connection.fetchval(
                """
                SELECT to_regclass('public.customer_upload_batches');
                """
            )
            files = await connection.fetchval(
                """
                SELECT to_regclass('public.customer_upload_batch_files');
                """
            )

        self.assertEqual(customers_path, 1)
        self.assertIsNotNone(batches)
        self.assertIsNotNone(files)

    async def test_customers_customer_path_column_exists(self) -> None:
        async with self.db.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'customers'
                  AND column_name = 'customer_path';
                """
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["data_type"], "text")
        self.assertEqual(row["is_nullable"], "YES")

    async def test_create_and_get_batch(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"batch-{uuid.uuid4()}",
            telegram_chat_id=1001,
            telegram_user_id=2002,
            media_group_id="mg-1",
        )
        self.assertEqual(batch["status"], CustomerUploadBatchStatus.COLLECTING)
        self.assertEqual(batch["telegram_chat_id"], 1001)

        by_id = await self.repo.get_batch_by_id(batch["id"])
        by_key = await self.repo.get_batch_by_key(batch["batch_key"])
        self.assertEqual(by_id["id"], batch["id"])
        self.assertEqual(by_key["id"], batch["id"])

    async def test_get_active_batch(self) -> None:
        older = await self.repo.create_batch(
            batch_key=f"old-{uuid.uuid4()}",
            telegram_chat_id=10,
            telegram_user_id=20,
        )
        await self.repo.update_batch_status(
            older["id"],
            CustomerUploadBatchStatus.ABANDONED,
        )
        failed = await self.repo.create_batch(
            batch_key=f"active-{uuid.uuid4()}",
            telegram_chat_id=10,
            telegram_user_id=20,
        )
        await self.repo.mark_batch_failed(failed["id"], "boom")
        still_active = await self.repo.create_batch(
            batch_key=f"live-{uuid.uuid4()}",
            telegram_chat_id=10,
            telegram_user_id=20,
        )

        found = await self.repo.get_active_batch(10, 20)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], still_active["id"])
        self.assertEqual(found["status"], CustomerUploadBatchStatus.COLLECTING)

        finished = await self.repo.update_batch_status(
            still_active["id"],
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        )
        self.assertEqual(
            finished["status"],
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        )
        self.assertIsNone(await self.repo.get_active_batch(10, 20))

    async def test_get_batch_by_media_group(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"mg-{uuid.uuid4()}",
            telegram_chat_id=11,
            telegram_user_id=22,
            media_group_id="album-42",
        )
        found = await self.repo.get_batch_by_media_group(11, 22, "album-42")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], batch["id"])
        self.assertIsNone(
            await self.repo.get_batch_by_media_group(11, 22, "missing")
        )

    async def test_add_file_is_idempotent_by_message_id(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"idem-{uuid.uuid4()}",
            telegram_chat_id=1,
            telegram_user_id=2,
        )
        first = await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=555,
            telegram_file_id="file-a",
            original_filename="a.jpg",
            mime_type="image/jpeg",
            file_extension=".jpg",
            file_size=10,
            temporary_content=b"one",
        )
        second = await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=555,
            telegram_file_id="file-b",
            original_filename="b.jpg",
            temporary_content=b"two",
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["telegram_file_id"], "file-a")
        self.assertEqual(second["temporary_content"], b"one")
        self.assertEqual(await self.repo.count_batch_files(batch["id"]), 1)

    async def test_bytea_roundtrip_and_clear(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"bytea-{uuid.uuid4()}",
            telegram_chat_id=1,
            telegram_user_id=2,
        )
        payload = b"\x00PDF-binary-\xff\xfe"
        file_row = await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=1,
            temporary_content=payload,
        )
        files = await self.repo.get_batch_files(batch["id"])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["temporary_content"], payload)

        cleared = await self.repo.clear_batch_file_contents(batch["id"])
        self.assertEqual(cleared, 1)
        files_after = await self.repo.get_batch_files(batch["id"])
        self.assertIsNone(files_after[0]["temporary_content"])
        self.assertEqual(files_after[0]["id"], file_row["id"])

    async def test_update_status_and_customer_path(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"status-{uuid.uuid4()}",
            telegram_chat_id=1,
            telegram_user_id=2,
        )
        updated = await self.repo.update_batch_status(
            batch["id"],
            CustomerUploadBatchStatus.RECOGNIZING,
        )
        self.assertEqual(
            updated["status"],
            CustomerUploadBatchStatus.RECOGNIZING,
        )
        self.assertIsNotNone(updated["processing_started_at"])

        with_path = await self.repo.set_batch_customer_path(
            batch["id"],
            "/Clients/Ivanov_1234",
        )
        self.assertEqual(with_path["customer_path"], "/Clients/Ivanov_1234")

        file_row = await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=77,
            temporary_content=b"x",
        )
        recognized = await self.repo.update_file_recognition(
            file_row["id"],
            detected_document_type="passport_main",
            recognition_status="recognized",
            extracted_json={"last_name": "Ivanov", "passport": "1234"},
        )
        self.assertEqual(recognized["detected_document_type"], "passport_main")
        self.assertEqual(recognized["extracted_json"]["last_name"], "Ivanov")

        with_disk = await self.repo.set_file_yadisk_path(
            file_row["id"],
            "/Clients/Ivanov_1234/passport_main.jpg",
        )
        self.assertEqual(
            with_disk["final_yadisk_path"],
            "/Clients/Ivanov_1234/passport_main.jpg",
        )

    async def test_cascade_delete_files_with_batch(self) -> None:
        batch = await self.repo.create_batch(
            batch_key=f"cascade-{uuid.uuid4()}",
            telegram_chat_id=1,
            telegram_user_id=2,
        )
        await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=1,
            temporary_content=b"a",
        )
        await self.repo.add_file(
            batch_id=batch["id"],
            telegram_message_id=2,
            temporary_content=b"b",
        )
        self.assertEqual(await self.repo.count_batch_files(batch["id"]), 2)

        async with self.db.pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM customer_upload_batches WHERE id = $1;",
                batch["id"],
            )
            remaining = await connection.fetchval(
                """
                SELECT COUNT(*)::bigint
                FROM customer_upload_batch_files
                WHERE batch_id = $1;
                """,
                batch["id"],
            )
        self.assertEqual(remaining, 0)

    async def test_create_customer_without_customer_path_still_works(self) -> None:
        created = await self.db.create_customer(
            {
                "passport": f"PASS-{uuid.uuid4().hex[:8]}",
                "first_name": "Ivan",
                "last_name": "Ivanov",
            }
        )
        self.assertIn("customer_path", created)
        self.assertIsNone(created["customer_path"])

        loaded = await self.db.get_customer_by_id(created["id"])
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded["customer_path"])

        with_path = await self.db.create_customer(
            {
                "passport": f"PASS-{uuid.uuid4().hex[:8]}",
                "last_name": "Petrov",
                "customer_path": "/Clients/Petrov_9999",
            }
        )
        self.assertEqual(with_path["customer_path"], "/Clients/Petrov_9999")

    async def test_delete_customer_clears_batch_customer_id(self) -> None:
        customer = await self.db.create_customer(
            {
                "passport": f"DEL-{uuid.uuid4().hex[:8]}",
                "first_name": "Ivan",
                "last_name": "Ivanov",
                "customer_path": "/Clients/Ivanov_1",
            }
        )
        batch = await self.repo.create_batch(
            batch_key=f"del-{uuid.uuid4().hex}",
            telegram_chat_id=1,
            telegram_user_id=2,
        )
        await self.repo.mark_batch_customer_saved(
            batch["id"],
            customer_id=int(customer["id"]),
        )
        deleted = await self.db.delete_customer_with_related_data(int(customer["id"]))
        self.assertTrue(deleted)
        self.assertIsNone(await self.db.get_customer_by_id(int(customer["id"])))
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertIsNotNone(refreshed)
        self.assertIsNone(refreshed["customer_id"])
        self.assertEqual(
            refreshed["status"],
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        )


class CustomerUploadBatchSqlConstantsTests(unittest.TestCase):
    def test_sql_constants_define_required_objects(self) -> None:
        from app import database as db_module

        self.assertIn("customer_path TEXT", db_module.CREATE_CUSTOMERS_TABLE_SQL)
        self.assertTrue(
            any(
                "customer_path TEXT" in statement
                for statement in db_module.ENSURE_CUSTOMERS_EXTRA_FIELDS_SQL
            )
        )
        self.assertIn(
            "customer_upload_batches",
            db_module.CREATE_CUSTOMER_UPLOAD_BATCHES_TABLE_SQL,
        )
        self.assertIn(
            "temporary_content BYTEA",
            db_module.CREATE_CUSTOMER_UPLOAD_BATCH_FILES_TABLE_SQL,
        )
        indexes = "\n".join(db_module.CREATE_INDEXES_SQL)
        self.assertIn("idx_customer_upload_batches_media_group", indexes)
        self.assertIn("idx_customer_upload_batches_active", indexes)
        self.assertIn("idx_customer_upload_batch_files_batch", indexes)


if __name__ == "__main__":
    unittest.main()
