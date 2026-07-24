from __future__ import annotations

import io
import time
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.config import Settings, parse_miniapp_allowed_telegram_user_ids
from app.database import Database
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.miniapp_batch_api_service import sniff_extension_and_mime
from app.services.validation_service import normalize_passport
from app.web.app import create_fastapi_app
from app.web.auth import build_telegram_init_data_for_tests
from app.web.token_service import (
    verify_customer_batch_context_token,
    verify_customer_edit_context_token,
    verify_specification_context_token,
)
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


BOT_TOKEN = "123456:ABC-DEF"
SECRET = "mini-app-secret"
NOW = int(time.time())
USER_ID = 4242
OTHER_USER = 9999
DENIED_USER = 7777


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
        "customer_upload_max_file_bytes": 2048,
        "telegram_bot_username": "demo_bot",
        "miniapp_test_mode": False,
        "miniapp_allowed_telegram_user_ids": frozenset({USER_ID}),
    }
    values.update(overrides)
    return Settings(**values)


def _init_data(*, user_id: int = USER_ID, auth_date: int | None = None) -> str:
    return build_telegram_init_data_for_tests(
        bot_token=BOT_TOKEN,
        user={"id": user_id, "first_name": "Ivan"},
        auth_date=auth_date if auth_date is not None else NOW - 5,
    )


TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x08"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xaa\xff\xd9"
)


class MiniAppShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = create_fastapi_app(
            settings=_settings(),
            database=MagicMock(),
            bot=MagicMock(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_miniapp_home_opens(self) -> None:
        response = await self.client.get("/miniapp")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Arthur AutoExport", response.text)

    async def test_miniapp_home_has_two_buttons(self) -> None:
        response = await self.client.get("/miniapp")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/miniapp.js", response.text)
        js = await self.client.get("/static/miniapp.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("Добавить клиента", js.text)
        self.assertIn("Найти клиента и сформировать документы", js.text)

    async def test_backbutton_hidden_on_home_in_js(self) -> None:
        js = await self.client.get("/static/miniapp.js")
        self.assertIn("BackButton.hide()", js.text)
        self.assertIn('path === "/miniapp"', js.text)


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class MiniAppApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.user_id = 400000 + (uuid.uuid4().int % 100000)
        self.settings = _settings(
            database_url=self.database_url,
            miniapp_allowed_telegram_user_ids=frozenset({self.user_id, OTHER_USER}),
        )
        recognition = AsyncMock()
        recognition.process_batch = AsyncMock()
        recognition.repository = self.repo
        folder = AsyncMock()
        folder.ensure_batch_files_saved = AsyncMock()
        folder.repository = self.repo
        self.app = create_fastapi_app(
            settings=self.settings,
            database=self.db,
            bot=MagicMock(),
            customer_batch_recognition_service=recognition,
            customer_folder_service=folder,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.db.close()

    def _init(self, user_id: int | None = None) -> str:
        return _init_data(user_id=user_id if user_id is not None else self.user_id)

    async def _create_batch(self, *, force_new: bool = False, user_id: int | None = None):
        uid = self.user_id if user_id is None else user_id
        response = await self.client.post(
            "/api/customer-batches",
            json={
                "telegram_init_data": self._init(uid),
                "force_new": force_new,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def _upload(
        self,
        batch_id: int,
        doc_type: str,
        *,
        content: bytes = TINY_JPEG,
        filename: str = "doc.jpg",
        content_type: str = "image/jpeg",
        user_id: int | None = None,
    ):
        uid = self.user_id if user_id is None else user_id
        response = await self.client.post(
            f"/api/customer-batches/{batch_id}/files",
            data={
                "declared_document_type": doc_type,
                "telegram_init_data": self._init(uid),
            },
            files={"file": (filename, io.BytesIO(content), content_type)},
        )
        return response

    async def test_creates_miniapp_batch_for_authenticated_user(self) -> None:
        payload = await self._create_batch()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        batch = payload["batch"]
        self.assertEqual(batch["origin"], "miniapp")
        row = await self.repo.get_batch_by_id(batch["batch_id"])
        self.assertEqual(int(row["telegram_user_id"]), self.user_id)

    async def test_upload_four_files_and_declared_type(self) -> None:
        payload = await self._create_batch()
        batch_id = payload["batch"]["batch_id"]
        for doc_type in (
            "passport_main",
            "passport_registration",
            "snils",
            "tin",
        ):
            response = await self._upload(batch_id, doc_type, filename=f"{doc_type}.jpg")
            self.assertEqual(response.status_code, 200, response.text)
        files = await self.repo.get_batch_files(batch_id)
        self.assertEqual(len(files), 4)
        types = {item["declared_document_type"] for item in files}
        self.assertEqual(
            types,
            {"passport_main", "passport_registration", "snils", "tin"},
        )
        status = await self.client.get(
            f"/api/customer-batches/{batch_id}/status",
            headers={"X-Telegram-Init-Data": self._init()},
        )
        self.assertTrue(status.json()["batch"]["can_recognize"])

    async def test_rejects_bad_mime_and_large_file(self) -> None:
        payload = await self._create_batch()
        batch_id = payload["batch"]["batch_id"]
        bad = await self._upload(
            batch_id,
            "passport_main",
            content=b"not-an-image",
            filename="x.bin",
            content_type="application/octet-stream",
        )
        self.assertEqual(bad.status_code, 400)
        large = await self._upload(
            batch_id,
            "passport_main",
            content=b"\xff\xd8\xff" + (b"0" * 5000),
            filename="big.jpg",
        )
        self.assertEqual(large.status_code, 400)
        self.assertEqual(large.json()["error"]["code"], "FILE_TOO_LARGE")

    async def test_foreign_user_cannot_upload(self) -> None:
        payload = await self._create_batch()
        batch_id = payload["batch"]["batch_id"]
        response = await self._upload(batch_id, "passport_main", user_id=OTHER_USER)
        self.assertEqual(response.status_code, 403)

    async def test_recognize_cannot_start_twice(self) -> None:
        payload = await self._create_batch()
        batch_id = payload["batch"]["batch_id"]
        for doc_type in (
            "passport_main",
            "passport_registration",
            "snils",
            "tin",
        ):
            await self._upload(batch_id, doc_type)
        first = await self.client.post(
            f"/api/customer-batches/{batch_id}/recognize",
            json={"telegram_init_data": self._init()},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["started"])
        second = await self.client.post(
            f"/api/customer-batches/{batch_id}/recognize",
            json={"telegram_init_data": self._init()},
        )
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["started"])

    async def test_active_batch_restored_and_abandoned_ignored(self) -> None:
        first = await self._create_batch()
        batch_id = first["batch"]["batch_id"]
        active = await self.client.get(
            "/api/customer-batches/active",
            headers={"X-Telegram-Init-Data": self._init()},
        )
        self.assertEqual(active.json()["batch"]["batch_id"], batch_id)
        await self.repo.update_batch_status(batch_id, CustomerUploadBatchStatus.ABANDONED)
        active2 = await self.client.get(
            "/api/customer-batches/active",
            headers={"X-Telegram-Init-Data": self._init()},
        )
        self.assertIsNone(active2.json()["batch"])

    async def test_files_saved_preview_and_form_token(self) -> None:
        payload = await self._create_batch()
        batch_id = payload["batch"]["batch_id"]
        await self.repo.add_file(
            batch_id=batch_id,
            original_filename="p.jpg",
            mime_type="image/jpeg",
            file_extension="jpg",
            file_size=10,
            temporary_content=TINY_JPEG,
            declared_document_type="passport_main",
        )
        await self.repo.update_batch_status(batch_id, CustomerUploadBatchStatus.FILES_SAVED)
        await self.repo.update_file_recognition(
            (await self.repo.get_batch_files(batch_id))[0]["id"],
            recognition_status="success",
            detected_document_type="passport_main",
            extracted_json={
                "fields": {
                    "last_name": "ИВАНОВ",
                    "first_name": "ИВАН",
                    "passport": "80 11 541410",
                    "birth_date": "12.06.1981",
                    "birth_place": "Р.П. АРЬЯ",
                }
            },
        )
        status = await self.client.get(
            f"/api/customer-batches/{batch_id}/status",
            headers={"X-Telegram-Init-Data": self._init()},
        )
        body = status.json()["batch"]
        self.assertTrue(body["can_open_form"])
        self.assertEqual(body["preview"]["birth_date"], "12.06.1981")
        token_resp = await self.client.post(
            "/api/miniapp/batch-form-token",
            json={"telegram_init_data": self._init(), "batch_id": batch_id},
        )
        self.assertEqual(token_resp.status_code, 200, token_resp.text)
        url = token_resp.json()["url"]
        self.assertTrue(url.startswith("https://example.com/miniapp/customer?token="))
        self.assertIn("from=miniapp", url)
        self.assertNotIn("ИВАНОВ", url)
        token = url.split("token=", 1)[1].split("&", 1)[0]
        context = verify_customer_batch_context_token(
            token,
            secret=SECRET,
            expected_telegram_user_id=self.user_id,
        )
        self.assertEqual(context.batch_id, batch_id)

    async def test_search_normalizes_and_finds_customer(self) -> None:
        digits = f"80{uuid.uuid4().int % 10**8:08d}"
        passport = normalize_passport(digits)
        customer = await self.db.create_customer(
            {
                "passport": passport,
                "last_name": "Иванов",
                "first_name": "Иван",
                "surname": None,
                "phone": None,
                "email": None,
            }
        )
        found = await self.client.post(
            "/api/customers/search",
            json={
                "telegram_init_data": self._init(),
                "passport": digits,
            },
        )
        self.assertEqual(found.status_code, 200)
        body = found.json()
        self.assertEqual(body["status"], "found")
        self.assertEqual(body["customer"]["customer_id"], customer["id"])
        values = {item["key"]: item["value"] for item in body["customer"]["fields"]}
        self.assertEqual(values["email"], "не заполнено")
        empty = await self.client.post(
            "/api/customers/search",
            json={"telegram_init_data": self._init(), "passport": "12"},
        )
        self.assertEqual(empty.status_code, 422)
        missing = await self.client.post(
            "/api/customers/search",
            json={"telegram_init_data": self._init(), "passport": "8011541411"},
        )
        self.assertEqual(missing.json()["status"], "not_found")

    async def test_search_requires_init_data_and_no_list_all(self) -> None:
        response = await self.client.post(
            "/api/customers/search",
            json={"telegram_init_data": "bad", "passport": "8011541410"},
        )
        self.assertEqual(response.status_code, 401)
        # Endpoint never returns a customers array.
        ok = await self.client.post(
            "/api/customers/search",
            json={"telegram_init_data": self._init(), "passport": "8011541410"},
        )
        self.assertNotIn("customers", ok.json())

    async def test_edit_and_specification_tokens(self) -> None:
        customer = await self.db.create_customer(
            {
                "passport": f"80 22 {uuid.uuid4().int % 10**6:06d}",
                "last_name": "Петров",
                "first_name": "Пётр",
            }
        )
        edit = await self.client.post(
            "/api/miniapp/customer-edit-token",
            json={
                "telegram_init_data": self._init(),
                "customer_id": customer["id"],
            },
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        edit_url = edit.json()["url"]
        self.assertNotIn("Петров", edit_url)
        edit_token = edit_url.split("token=", 1)[1].split("&", 1)[0]
        edit_ctx = verify_customer_edit_context_token(
            edit_token,
            secret=SECRET,
            expected_telegram_user_id=self.user_id,
        )
        self.assertEqual(edit_ctx.customer_id, customer["id"])

        spec = await self.client.post(
            "/api/miniapp/specification-token",
            json={
                "telegram_init_data": self._init(),
                "customer_id": customer["id"],
            },
        )
        self.assertEqual(spec.status_code, 200, spec.text)
        spec_token = spec.json()["url"].split("token=", 1)[1].split("&", 1)[0]
        spec_ctx = verify_specification_context_token(
            spec_token,
            secret=SECRET,
            expected_telegram_user_id=self.user_id,
        )
        self.assertEqual(spec_ctx.customer_id, customer["id"])

    async def test_invalid_and_expired_init_data(self) -> None:
        invalid = await self.client.post(
            "/api/miniapp/bootstrap",
            json={"telegram_init_data": "user=%7B%22id%22%3A1%7D&hash=00"},
        )
        self.assertEqual(invalid.status_code, 401)
        expired = await self.client.post(
            "/api/miniapp/bootstrap",
            json={
                "telegram_init_data": _init_data(
                    user_id=self.user_id,
                    auth_date=NOW - 10_000,
                ),
            },
        )
        self.assertEqual(expired.status_code, 401)

    async def test_user_id_spoof_blocked_by_signature(self) -> None:
        # Crafted payload claiming OTHER_USER but signed for USER_ID is impossible
        # without bot token; unsigned spoof fails hash check.
        response = await self.client.post(
            "/api/customer-batches",
            json={
                "telegram_init_data": (
                    "user=%7B%22id%22%3A9999%7D&auth_date="
                    + str(NOW - 5)
                    + "&hash="
                    + ("ab" * 32)
                )
            },
        )
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class MiniAppAclTests(unittest.IsolatedAsyncioTestCase):
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
        self.allowed_user = 510000 + (uuid.uuid4().int % 10000)
        self.denied_user = DENIED_USER
        self.settings = _settings(
            database_url=self.database_url,
            miniapp_allowed_telegram_user_ids=frozenset({self.allowed_user}),
            miniapp_test_mode=False,
        )
        recognition = AsyncMock()
        recognition.process_batch = AsyncMock()
        recognition.repository = CustomerUploadBatchRepository(self.db.pool)
        folder = AsyncMock()
        folder.ensure_batch_files_saved = AsyncMock()
        folder.repository = recognition.repository
        self.app = create_fastapi_app(
            settings=self.settings,
            database=self.db,
            bot=MagicMock(),
            customer_batch_recognition_service=recognition,
            customer_folder_service=folder,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.db.close()

    def _init(self, user_id: int) -> str:
        return _init_data(user_id=user_id)

    async def test_allowlisted_user_opens_bootstrap(self) -> None:
        response = await self.client.post(
            "/api/miniapp/bootstrap",
            json={"telegram_init_data": self._init(self.allowed_user)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    async def test_user_outside_allowlist_gets_403(self) -> None:
        response = await self.client.post(
            "/api/miniapp/bootstrap",
            json={"telegram_init_data": self._init(self.denied_user)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "MINIAPP_FORBIDDEN")

    async def test_valid_init_data_without_allowlist_is_not_enough(self) -> None:
        response = await self.client.post(
            "/api/customers/search",
            json={
                "telegram_init_data": self._init(self.denied_user),
                "passport": "8011541410",
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_empty_allowlist_in_production_denies_access(self) -> None:
        locked = create_fastapi_app(
            settings=_settings(
                database_url=self.database_url,
                miniapp_allowed_telegram_user_ids=frozenset(),
                miniapp_test_mode=False,
            ),
            database=self.db,
            bot=MagicMock(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=locked),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/miniapp/bootstrap",
                json={"telegram_init_data": self._init(self.allowed_user)},
            )
        self.assertEqual(response.status_code, 403)

    async def test_test_mode_does_not_bypass_empty_allowlist(self) -> None:
        locked = create_fastapi_app(
            settings=_settings(
                database_url=self.database_url,
                miniapp_allowed_telegram_user_ids=frozenset(),
                miniapp_test_mode=True,
            ),
            database=self.db,
            bot=MagicMock(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=locked),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/miniapp/bootstrap",
                json={"telegram_init_data": self._init(self.allowed_user)},
            )
        self.assertEqual(response.status_code, 403)

    async def test_allowlisted_user_can_search_customer(self) -> None:
        digits = f"80{uuid.uuid4().int % 10**8:08d}"
        passport = normalize_passport(digits)
        await self.db.create_customer(
            {
                "passport": passport,
                "last_name": "Сидоров",
                "first_name": "Сидор",
            }
        )
        response = await self.client.post(
            "/api/customers/search",
            json={
                "telegram_init_data": self._init(self.allowed_user),
                "passport": digits,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "found")

    async def test_denied_user_cannot_search_customer(self) -> None:
        response = await self.client.post(
            "/api/customers/search",
            json={
                "telegram_init_data": self._init(self.denied_user),
                "passport": "8011541410",
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_denied_user_cannot_open_customer_card(self) -> None:
        customer = await self.db.create_customer(
            {
                "passport": f"80 33 {uuid.uuid4().int % 10**6:06d}",
                "last_name": "Тестов",
                "first_name": "Тест",
            }
        )
        response = await self.client.post(
            "/api/miniapp/customer-card",
            json={
                "telegram_init_data": self._init(self.denied_user),
                "customer_id": customer["id"],
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_denied_user_cannot_get_edit_token(self) -> None:
        customer = await self.db.create_customer(
            {
                "passport": f"80 44 {uuid.uuid4().int % 10**6:06d}",
                "last_name": "Тестов",
                "first_name": "Тест",
            }
        )
        response = await self.client.post(
            "/api/miniapp/customer-edit-token",
            json={
                "telegram_init_data": self._init(self.denied_user),
                "customer_id": customer["id"],
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_denied_user_cannot_get_specification_token(self) -> None:
        customer = await self.db.create_customer(
            {
                "passport": f"80 55 {uuid.uuid4().int % 10**6:06d}",
                "last_name": "Тестов",
                "first_name": "Тест",
            }
        )
        response = await self.client.post(
            "/api/miniapp/specification-token",
            json={
                "telegram_init_data": self._init(self.denied_user),
                "customer_id": customer["id"],
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_denied_user_cannot_create_batch(self) -> None:
        response = await self.client.post(
            "/api/customer-batches",
            json={"telegram_init_data": self._init(self.denied_user)},
        )
        self.assertEqual(response.status_code, 403)


class MiniAppAclUnitTests(unittest.TestCase):
    def test_test_mode_defaults_false_and_does_not_auto_enable(self) -> None:
        settings = _settings(miniapp_allowed_telegram_user_ids=frozenset())
        self.assertFalse(settings.miniapp_test_mode)

    def test_parse_allowlist_ignores_spaces_and_rejects_invalid(self) -> None:
        parsed = parse_miniapp_allowed_telegram_user_ids(" 316257868, 123456789 ,987654321 ")
        self.assertEqual(parsed, frozenset({316257868, 123456789, 987654321}))
        self.assertEqual(parse_miniapp_allowed_telegram_user_ids(""), frozenset())
        self.assertEqual(parse_miniapp_allowed_telegram_user_ids(None), frozenset())
        with self.assertRaises(RuntimeError):
            parse_miniapp_allowed_telegram_user_ids("1,abc,2")

    def test_add_customer_command_unaffected_by_empty_allowlist(self) -> None:
        from aiogram.filters import Command

        from app.handlers.customer_batch_upload import router as batch_router

        settings = _settings(miniapp_allowed_telegram_user_ids=frozenset())
        self.assertEqual(settings.miniapp_allowed_telegram_user_ids, frozenset())
        batch_commands: list[set[str]] = []
        for observer in batch_router.message.handlers:
            for filter_obj in observer.filters or []:
                callback = getattr(filter_obj, "callback", None)
                if isinstance(callback, Command):
                    batch_commands.append(set(callback.commands))
        self.assertTrue(any("add_customer" in commands for commands in batch_commands))


class MiniAppUnitHelpersTests(unittest.TestCase):
    def test_passport_normalization(self) -> None:
        self.assertEqual(normalize_passport("80 11 541410"), "80 11 541410")
        self.assertEqual(normalize_passport("8011541410"), "80 11 541410")
        self.assertEqual(normalize_passport("80-11-541410"), "80 11 541410")
        self.assertIsNone(normalize_passport("123"))

    def test_sniff_jpeg(self) -> None:
        extension, mime = sniff_extension_and_mime(
            filename="x.jpg",
            content_type="image/jpeg",
            content=TINY_JPEG,
        )
        self.assertEqual(extension, "jpg")
        self.assertEqual(mime, "image/jpeg")


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class MiniAppRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
        self.user_id = 420000 + (uuid.uuid4().int % 100000)
        self.settings = _settings(
            database_url=self.database_url,
            miniapp_allowed_telegram_user_ids=frozenset({self.user_id, OTHER_USER}),
            customer_batch_stale_processing_seconds=60,
        )
        self.recognition = AsyncMock()
        self.recognition.process_batch = AsyncMock()
        self.recognition.repository = self.repo
        self.folder = AsyncMock()
        self.folder.ensure_batch_files_saved = AsyncMock()
        self.folder.repository = self.repo
        self.app = create_fastapi_app(
            settings=self.settings,
            database=self.db,
            bot=MagicMock(),
            customer_batch_recognition_service=self.recognition,
            customer_folder_service=self.folder,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.db.close()

    def _init(self, user_id: int | None = None) -> str:
        return _init_data(user_id=user_id if user_id is not None else self.user_id)

    async def _create_ready_batch(self) -> int:
        response = await self.client.post(
            "/api/customer-batches",
            json={"telegram_init_data": self._init()},
        )
        self.assertEqual(response.status_code, 200, response.text)
        batch_id = response.json()["batch"]["batch_id"]
        for doc_type in (
            "passport_main",
            "passport_registration",
            "snils",
            "tin",
        ):
            upload = await self.client.post(
                f"/api/customer-batches/{batch_id}/files",
                data={
                    "declared_document_type": doc_type,
                    "telegram_init_data": self._init(),
                },
                files={"file": (f"{doc_type}.jpg", io.BytesIO(TINY_JPEG), "image/jpeg")},
            )
            self.assertEqual(upload.status_code, 200, upload.text)
        return batch_id

    async def _force_recognizing(
        self,
        batch_id: int,
        *,
        seconds_ago: int,
        origin: str = "miniapp",
    ) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    origin = $3,
                    processing_started_at = NOW() - make_interval(secs => $4),
                    updated_at = NOW() - make_interval(secs => $4),
                    error_message = NULL
                WHERE id = $1
                """,
                batch_id,
                CustomerUploadBatchStatus.RECOGNIZING,
                origin,
                int(seconds_ago),
            )

    async def test_claim_sets_processing_started_at(self) -> None:
        batch_id = await self._create_ready_batch()
        claimed = await self.repo.claim_batch_for_processing(batch_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], CustomerUploadBatchStatus.RECOGNIZING)
        self.assertIsNotNone(claimed["processing_started_at"])

    async def test_fresh_recognizing_batch_is_not_recovered(self) -> None:
        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=5)
        recovered = await self.repo.recover_stale_recognizing_batch(
            batch_id,
            stale_after_seconds=60,
            origin="miniapp",
        )
        self.assertIsNone(recovered)
        row = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(row["status"], CustomerUploadBatchStatus.RECOGNIZING)

    async def test_stale_recognizing_batch_becomes_collecting(self) -> None:
        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=600)
        recovered = await self.repo.recover_stale_recognizing_batch(
            batch_id,
            stale_after_seconds=60,
            origin="miniapp",
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], CustomerUploadBatchStatus.COLLECTING)
        self.assertEqual(recovered["error_message"], "processing_interrupted")

    async def test_recovery_update_is_atomic(self) -> None:
        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=600)
        first = await self.repo.recover_stale_recognizing_batch(
            batch_id,
            stale_after_seconds=60,
            origin="miniapp",
        )
        second = await self.repo.recover_stale_recognizing_batch(
            batch_id,
            stale_after_seconds=60,
            origin="miniapp",
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_foreign_user_cannot_recover_via_status(self) -> None:
        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=600)
        response = await self.client.get(
            f"/api/customer-batches/{batch_id}/status",
            headers={"X-Telegram-Init-Data": self._init(OTHER_USER)},
        )
        self.assertEqual(response.status_code, 403)
        row = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(row["status"], CustomerUploadBatchStatus.RECOGNIZING)

    async def test_status_returns_processing_interrupted_and_allows_retry(self) -> None:
        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=600)
        status = await self.client.get(
            f"/api/customer-batches/{batch_id}/status",
            headers={"X-Telegram-Init-Data": self._init()},
        )
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()["batch"]
        self.assertEqual(body["status"], CustomerUploadBatchStatus.COLLECTING)
        self.assertEqual(body["error_code"], "PROCESSING_INTERRUPTED")
        self.assertIn("перезапуском", body["error_message"])
        self.assertTrue(body["can_recognize"])

        before_files = await self.repo.get_batch_files(batch_id)
        retry = await self.client.post(
            f"/api/customer-batches/{batch_id}/recognize",
            json={"telegram_init_data": self._init()},
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["started"])
        after_files = await self.repo.get_batch_files(batch_id)
        self.assertEqual(len(before_files), 4)
        self.assertEqual(len(after_files), 4)

    async def test_background_exception_marks_failed(self) -> None:
        from app.services.miniapp_batch_api_service import run_batch_processing

        batch_id = await self._create_ready_batch()
        await self.repo.claim_batch_for_processing(batch_id)
        self.recognition.process_batch = AsyncMock(side_effect=RuntimeError("boom"))
        await run_batch_processing(
            batch_id=batch_id,
            recognition_service=self.recognition,
            folder_service=self.folder,
        )
        row = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(row["status"], CustomerUploadBatchStatus.FAILED)
        self.assertEqual(row["error_message"], "processing_failed")

    async def test_startup_recovery_simulation(self) -> None:
        from app.services.miniapp_batch_api_service import (
            recover_stale_miniapp_batches_on_startup,
        )

        batch_id = await self._create_ready_batch()
        await self._force_recognizing(batch_id, seconds_ago=600)
        recovered = await recover_stale_miniapp_batches_on_startup(
            self.repo,
            stale_after_seconds=60,
        )
        recovered_ids = {int(item["id"]) for item in recovered}
        self.assertIn(batch_id, recovered_ids)
        row = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(row["status"], CustomerUploadBatchStatus.COLLECTING)
        self.assertEqual(row["error_message"], "processing_interrupted")

    async def test_terminal_batch_recovery_ignored(self) -> None:
        batch_id = await self._create_ready_batch()
        await self.repo.update_batch_status(
            batch_id,
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        )
        async with self.db.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE customer_upload_batches
                SET processing_started_at = NOW() - make_interval(secs => 600)
                WHERE id = $1
                """,
                batch_id,
            )
        recovered = await self.repo.recover_stale_recognizing_batch(
            batch_id,
            stale_after_seconds=60,
            origin="miniapp",
        )
        self.assertIsNone(recovered)
        row = await self.repo.get_batch_by_id(batch_id)
        self.assertEqual(row["status"], CustomerUploadBatchStatus.CUSTOMER_SAVED)


class MiniAppRegressionPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = create_fastapi_app(
            settings=_settings(),
            database=MagicMock(),
            bot=MagicMock(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_existing_customer_and_specification_pages(self) -> None:
        customer = await self.client.get("/miniapp/customer")
        specification = await self.client.get("/miniapp/specification")
        self.assertEqual(customer.status_code, 200)
        self.assertEqual(specification.status_code, 200)
        self.assertIn("Данные клиента", customer.text)
        self.assertIn("specification", specification.text.lower())


if __name__ == "__main__":
    unittest.main()
