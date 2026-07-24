from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import Database
from app.handlers.customer_batch_upload import handle_batch_process
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchFileRecognitionStatus,
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_recognition_service import (
    CustomerBatchRecognitionService,
    format_kit_telegram_message,
    validate_document_kit,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionResult,
    CustomerDocumentRecognitionService,
)
from app.services.customer_folder_service import FolderFinalizeResult
from app.services.yandex_ocr_service import YandexOCRService, _retry_delay_seconds
from app.config import Settings
from tests.postgres_test_utils import (
    ensure_postgres_on_path,
    resolve_test_database_url,
)


def _mock_state(batch_id: int | None = None) -> AsyncMock:
    state = AsyncMock()
    data = {"batch_id": batch_id} if batch_id is not None else {}

    async def get_data() -> dict:
        return dict(data)

    async def update_data(**kwargs) -> None:
        data.update(kwargs)

    async def set_state(_value) -> None:
        return None

    state.get_data = get_data
    state.update_data = update_data
    state.set_state = set_state
    state._data = data
    return state


def _callback(*, chat_id: int = 100, user_id: int = 200) -> MagicMock:
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = MagicMock()
    callback.message.chat = SimpleNamespace(id=chat_id)
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


class KitValidationUnitTests(unittest.TestCase):
    def test_detects_missing_duplicate_mixed_unknown(self) -> None:
        files = [
            {
                "id": 1,
                "recognition_status": "success",
                "detected_document_type": "passport_main",
                "extracted_json": {
                    "document_type": "passport_main",
                    "fields": {"last_name": "Ivanov", "first_name": "Ivan", "passport": "80 06 035956"},
                },
            },
            {
                "id": 2,
                "recognition_status": "success",
                "detected_document_type": "passport_registration",
                "extracted_json": {"document_type": "passport_registration", "fields": {}},
            },
            {
                "id": 3,
                "recognition_status": "success",
                "detected_document_type": "snils",
                "extracted_json": {
                    "document_type": "snils",
                    "fields": {"last_name": "Ivanov", "first_name": "Ivan"},
                },
            },
            {
                "id": 4,
                "recognition_status": "success",
                "detected_document_type": "snils",
                "extracted_json": {
                    "document_type": "snils",
                    "fields": {"last_name": "Ivanov", "first_name": "Ivan"},
                },
            },
            {
                "id": 5,
                "recognition_status": "success",
                "detected_document_type": "mixed",
                "extracted_json": {"document_type": "mixed"},
            },
            {
                "id": 6,
                "recognition_status": "success",
                "detected_document_type": "unknown",
                "extracted_json": {"document_type": "unknown"},
            },
            {
                "id": 7,
                "recognition_status": "failed",
                "detected_document_type": None,
                "extracted_json": None,
            },
        ]
        kit = validate_document_kit(files)
        self.assertFalse(kit.is_complete)
        self.assertEqual(kit.missing_types, ["tin"])
        self.assertEqual(kit.duplicate_types, ["snils"])
        self.assertEqual(kit.mixed_file_ids, [5])
        self.assertEqual(kit.unknown_file_ids, [6])
        self.assertEqual(kit.failed_file_ids, [7])
        message = format_kit_telegram_message(kit)
        self.assertIn("требует проверки", message)
        self.assertIn("⚠️ СНИЛС", message)
        self.assertIn("❌ ИНН", message)

    def test_fio_mismatch_creates_warning(self) -> None:
        files = [
            {
                "id": 1,
                "recognition_status": "success",
                "detected_document_type": "passport_main",
                "extracted_json": {
                    "fields": {
                        "last_name": "Ivanov",
                        "first_name": "Ivan",
                        "passport": "80 06 035956",
                    }
                },
            },
            {
                "id": 2,
                "recognition_status": "success",
                "detected_document_type": "passport_registration",
                "extracted_json": {"fields": {"passport": "80 06 035956"}},
            },
            {
                "id": 3,
                "recognition_status": "success",
                "detected_document_type": "snils",
                "extracted_json": {
                    "fields": {"last_name": "Petrov", "first_name": "Petr"}
                },
            },
            {
                "id": 4,
                "recognition_status": "success",
                "detected_document_type": "tin",
                "extracted_json": {
                    "fields": {"last_name": "Ivanov", "first_name": "Ivan", "tin": "1"}
                },
            },
        ]
        kit = validate_document_kit(files)
        self.assertTrue(kit.is_complete)
        self.assertTrue(
            any("СНИЛС" in warning for warning in kit.warnings)
        )

    def test_ocr_retry_delays(self) -> None:
        self.assertEqual(_retry_delay_seconds(0), 1.0)
        self.assertEqual(_retry_delay_seconds(1), 2.0)
        self.assertEqual(_retry_delay_seconds(2), 4.0)


class RecognizeDocumentUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_ocr_called_once_per_document(self) -> None:
        ocr = AsyncMock()
        ocr.recognize_text = AsyncMock(return_value="PASSPORT OCR TEXT")
        gpt = AsyncMock()
        gpt.complete = AsyncMock(
            side_effect=[
                '{"document_type":"passport_main","confidence":0.9,"reason":null,"detected_documents":[]}',
                '{"document_type":"passport_main","passport":"80 06 035956","last_name":"Ivanov","first_name":"Ivan","surname":null,"by_whom_issued":"МВД","date_issue":"01.01.2020","department_code":"020-001","confidence":{},"warnings":[]}',
            ]
        )
        service = CustomerDocumentRecognitionService(ocr_service=ocr, gpt_service=gpt)
        result = await service.recognize_document(b"img", "image/jpeg", "a.jpg")
        self.assertEqual(result.document_type, "passport_main")
        self.assertEqual(ocr.recognize_text.await_count, 1)
        self.assertEqual(result.extracted_fields.get("last_name"), "Ivanov")


@unittest.skipUnless(
    resolve_test_database_url() is not None,
    "Need TEST_DATABASE_URL or local postgres + testing.postgresql",
)
class CustomerBatchRecognitionFlowTests(unittest.IsolatedAsyncioTestCase):
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
        self.recognition = AsyncMock()
        self.service = CustomerBatchRecognitionService(
            repository=self.repo,
            recognition_service=self.recognition,
        )

    async def asyncTearDown(self) -> None:
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE customer_upload_batches CASCADE;")
        await self.db.close()

    async def _create_batch_with_files(
        self,
        *,
        status: str = CustomerUploadBatchStatus.COLLECTING,
        contents: list[bytes] | None = None,
    ) -> dict:
        batch = await self.repo.create_batch(
            batch_key=f"rec-{uuid.uuid4().hex}",
            telegram_chat_id=100,
            telegram_user_id=200,
        )
        payloads = contents or [b"a", b"b", b"c", b"d"]
        for index, content in enumerate(payloads, start=1):
            await self.repo.add_file(
                batch_id=batch["id"],
                telegram_message_id=index,
                temporary_content=content,
                original_filename=f"file_{index}.jpg",
                mime_type="image/jpeg",
                file_extension="jpg",
            )
        if status != CustomerUploadBatchStatus.COLLECTING:
            if status == CustomerUploadBatchStatus.RECOGNIZING:
                await self.repo.claim_batch_for_processing(batch["id"])
            else:
                await self.repo.update_batch_status(batch["id"], status)
        return await self.repo.get_batch_by_id(batch["id"])

    def _recognition_side_effect(self, mapping: dict[bytes, CustomerDocumentRecognitionResult]):
        async def _recognize(content, mime_type, filename):
            return mapping[content]

        return _recognize

    async def test_four_files_processed_sequentially_and_typed(self) -> None:
        batch = await self._create_batch_with_files()
        call_order: list[bytes] = []

        async def recognize(content, mime_type, filename):
            call_order.append(content)
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "80 06 035956"}),
                b"b": ("passport_registration", {"passport": "80 06 035956"}),
                b"c": ("snils", {"last_name": "Ivanov", "first_name": "Ivan", "ipain": "123-456-789 00"}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan", "tin": "123456789012"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="text",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)
        self.assertTrue(result.kit.is_complete)
        self.assertEqual(call_order, [b"a", b"b", b"c", b"d"])
        files = await self.repo.get_batch_files(batch["id"])
        self.assertEqual(
            {row["detected_document_type"] for row in files},
            {"passport_main", "passport_registration", "snils", "tin"},
        )
        self.assertTrue(all(row["extracted_json"] for row in files))
        self.assertIn("Данные готовы", result.telegram_message)

    async def test_success_file_not_recognized_again(self) -> None:
        batch = await self._create_batch_with_files()
        files = await self.repo.get_batch_files(batch["id"])
        await self.repo.mark_file_recognized(
            files[0]["id"],
            detected_document_type="passport_main",
            extracted_json={
                "document_type": "passport_main",
                "fields": {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"},
            },
        )

        async def recognize(content, mime_type, filename):
            mapping = {
                b"b": ("passport_registration", {}),
                b"c": ("snils", {"last_name": "Ivanov", "first_name": "Ivan"}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        await self.service.process_batch(batch["id"])
        self.assertEqual(self.recognition.recognize_document.await_count, 3)

    async def test_processing_and_failed_can_be_retried(self) -> None:
        batch = await self._create_batch_with_files(
            status=CustomerUploadBatchStatus.RECOGNIZING
        )
        files = await self.repo.get_batch_files(batch["id"])
        await self.repo.mark_file_processing(files[0]["id"])
        await self.repo.mark_file_recognition_failed(files[1]["id"], "boom")
        await self.repo.mark_file_recognized(
            files[2]["id"],
            detected_document_type="snils",
            extracted_json={
                "document_type": "snils",
                "fields": {"last_name": "Ivanov", "first_name": "Ivan"},
            },
        )

        async def recognize(content, mime_type, filename):
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"}),
                b"b": ("passport_registration", {}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)
        self.assertEqual(self.recognition.recognize_document.await_count, 3)
        self.assertTrue(result.kit.is_complete)

    async def test_recognizing_batch_resumes_without_collecting(self) -> None:
        batch = await self._create_batch_with_files(
            status=CustomerUploadBatchStatus.RECOGNIZING
        )

        async def recognize(content, mime_type, filename):
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"}),
                b"b": ("passport_registration", {}),
                b"c": ("snils", {"last_name": "Ivanov", "first_name": "Ivan"}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.RECOGNIZED)

    async def test_recognized_batch_is_not_reprocessed(self) -> None:
        batch = await self._create_batch_with_files(
            status=CustomerUploadBatchStatus.RECOGNIZED
        )
        self.recognition.recognize_document = AsyncMock()
        result = await self.service.process_batch(batch["id"])
        self.recognition.recognize_document.assert_not_awaited()
        self.assertEqual(result.processed_file_count, 0)

    async def test_one_file_error_keeps_other_results(self) -> None:
        batch = await self._create_batch_with_files()

        async def recognize(content, mime_type, filename):
            if content == b"c":
                raise RuntimeError("ocr down")
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"}),
                b"b": ("passport_registration", {}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        files = await self.repo.get_batch_files(batch["id"])
        by_content = {row["temporary_content"]: row for row in files}
        self.assertEqual(
            by_content[b"a"]["recognition_status"],
            CustomerUploadBatchFileRecognitionStatus.SUCCESS,
        )
        self.assertEqual(
            by_content[b"c"]["recognition_status"],
            CustomerUploadBatchFileRecognitionStatus.FAILED,
        )
        self.assertIsNotNone(by_content[b"c"]["error_message"])
        self.assertFalse(result.kit.is_complete)
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)

    async def test_all_ocr_failures_mark_batch_failed(self) -> None:
        batch = await self._create_batch_with_files()

        async def recognize(content, mime_type, filename):
            raise RuntimeError("Yandex OCR error 429")

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        self.assertTrue(result.is_technical_failure)
        self.assertEqual(result.status, CustomerUploadBatchStatus.FAILED)
        self.assertIn("Не удалось завершить", result.telegram_message)

    async def test_incomplete_kit_message(self) -> None:
        batch = await self._create_batch_with_files()

        async def recognize(content, mime_type, filename):
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"}),
                b"b": ("passport_registration", {}),
                b"c": ("snils", {"last_name": "Ivanov", "first_name": "Ivan"}),
                b"d": ("unknown", {}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.4,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        result = await self.service.process_batch(batch["id"])
        self.assertEqual(result.status, CustomerUploadBatchStatus.RECOGNIZED)
        self.assertIn("требует проверки", result.telegram_message)
        self.assertIn("❌ ИНН", result.telegram_message)

    async def test_callback_runs_recognition_and_reports_success(self) -> None:
        batch = await self._create_batch_with_files()

        async def recognize(content, mime_type, filename):
            mapping = {
                b"a": ("passport_main", {"last_name": "Ivanov", "first_name": "Ivan", "passport": "1"}),
                b"b": ("passport_registration", {}),
                b"c": ("snils", {"last_name": "Ivanov", "first_name": "Ivan"}),
                b"d": ("tin", {"last_name": "Ivanov", "first_name": "Ivan"}),
            }
            doc_type, fields = mapping[content]
            return CustomerDocumentRecognitionResult(
                document_type=doc_type,
                confidence=0.9,
                ocr_text="x",
                extracted_fields=fields,
                warnings=[],
            )

        self.recognition.recognize_document = AsyncMock(side_effect=recognize)
        service = CustomerBatchRecognitionService(
            repository=self.repo,
            recognition_service=self.recognition,
        )
        callback = _callback()
        bot = AsyncMock()
        state = _mock_state(batch["id"])
        folder_service = AsyncMock()
        folder_service.ensure_batch_files_saved = AsyncMock(
            return_value=FolderFinalizeResult(
                batch_id=batch["id"],
                customer_path="/base/02_Клиенты/Ivanov_1",
                uploaded_count=4,
                skipped_count=0,
                failed_count=0,
                status=CustomerUploadBatchStatus.FILES_SAVED,
            )
        )
        settings = Settings(
            telegram_bot_token="1:TEST",
            database_url=self.database_url,
            yandex_disk_token="token",
            yandex_disk_base_path="/base",
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
        await handle_batch_process(
            callback,
            state,
            bot,
            settings,
            self.repo,
            service,
            folder_service,
        )
        texts = [call.args[0] for call in callback.message.answer.await_args_list]
        self.assertTrue(any("Распознаю документы" in text for text in texts))
        self.assertTrue(any("Распознанные данные клиента" in text for text in texts))
        refreshed = await self.repo.get_batch_by_id(batch["id"])
        self.assertEqual(refreshed["status"], CustomerUploadBatchStatus.RECOGNIZED)
        folder_service.ensure_batch_files_saved.assert_awaited()

    async def test_ocr_429_retries_limited(self) -> None:
        delays: list[float] = []

        class FakeResponse:
            def __init__(self, status: int):
                self.status = status

            async def text(self):
                return "rate limit"

            async def json(self):
                return {"result": {"textAnnotation": {"fullText": "ok"}}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    return FakeResponse(429)
                return FakeResponse(200)

        service = YandexOCRService(api_key="x", max_retries=3, min_delay_seconds=1.5)
        session = FakeSession()

        async def fake_sleep(delay):
            delays.append(delay)

        with (
            patch("app.services.yandex_ocr_service.aiohttp.ClientSession", return_value=session),
            patch("app.services.yandex_ocr_service.asyncio.sleep", side_effect=fake_sleep),
        ):
            text = await service._recognize_image(b"jpeg", "image/jpeg")

        self.assertEqual(text, "ok")
        self.assertEqual(session.calls, 3)
        self.assertEqual(delays, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
