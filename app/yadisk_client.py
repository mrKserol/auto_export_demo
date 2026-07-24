from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import logging
from urllib.parse import quote

import aiohttp


YANDEX_DISK_API_URL = "https://cloud-api.yandex.net/v1/disk/resources"
INTAKE_FOLDER = "01_Входящие_Telegram"
CUSTOMERS_FOLDER = "02_Клиенты"
CUSTOMERS_INTAKE_SUBFOLDER = "01_Входящие"
CASES_FOLDER = "03_Сделки"

_RETRYABLE_HTTP_STATUSES = frozenset({423, 429, 500, 502, 503, 504})
_RETRY_DELAYS_SECONDS = (1, 2, 4, 8)
_MAX_RETRY_ATTEMPTS = 4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YandexDiskClient:
    token: str
    base_path: str

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self.token}"}

    async def ensure_base_path(self) -> None:
        async with aiohttp.ClientSession(headers=self._headers) as session:
            await self._create_directories(session, self.base_path)

    async def upload_bytes(
        self,
        path: str,
        content: bytes,
        *,
        overwrite: bool = True,
    ) -> str:
        disk_path = self._normalize_path(path)
        await self._ensure_parent_directory(disk_path)

        async with aiohttp.ClientSession(headers=self._headers) as session:
            upload_url = await self._get_upload_url(
                session,
                disk_path,
                overwrite=overwrite,
            )
            await self._put_upload_content(session, upload_url, content)

        return disk_path

    async def path_exists(self, path: str) -> bool:
        disk_path = self._normalize_path(path)
        url = f"{YANDEX_DISK_API_URL}?path={quote(disk_path, safe='')}"
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return True
                if response.status == 404:
                    return False
                response.raise_for_status()
        return False

    async def try_create_directory(self, path: str) -> bool:
        """Create a directory. Return True if newly created, False if it already exists."""
        disk_path = self._normalize_path(path)
        parent = disk_path.rsplit("/", 1)[0]
        async with aiohttp.ClientSession(headers=self._headers) as session:
            if parent and parent != "/":
                await self._create_directories(session, parent)
            return await self._put_directory(session, disk_path)

    async def ensure_directory(self, path: str) -> None:
        async with aiohttp.ClientSession(headers=self._headers) as session:
            await self._create_directories(session, path)

    async def move_resource(
        self,
        from_path: str,
        to_path: str,
        overwrite: bool = True,
    ) -> str:
        source_path = self._normalize_path(from_path)
        destination_path = self._normalize_path(to_path)
        await self._ensure_parent_directory(destination_path)

        url = (
            f"{YANDEX_DISK_API_URL}/move"
            f"?from={quote(source_path, safe='')}"
            f"&path={quote(destination_path, safe='')}"
            f"&overwrite={str(overwrite).lower()}"
        )
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(url) as response:
                response.raise_for_status()

        return destination_path

    def build_customer_folder_path(self, folder_name: str) -> str:
        return f"{self.base_path}/{CUSTOMERS_FOLDER}/{folder_name}"

    def build_customer_file_path(self, folder_name: str, file_name: str) -> str:
        return f"{self.build_customer_folder_path(folder_name)}/{file_name}"

    async def _ensure_parent_directory(self, path: str) -> None:
        parent = path.rsplit("/", 1)[0]
        if parent:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                await self._create_directories(session, parent)

    async def _create_directories(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> None:
        current = ""
        for part in self._normalize_path(path).strip("/").split("/"):
            current = f"{current}/{part}"
            await self._create_directory(session, current)

    async def _create_directory(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> None:
        await self._put_directory(session, path)

    async def _put_directory(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> bool:
        url = f"{YANDEX_DISK_API_URL}?path={quote(self._normalize_path(path), safe='')}"

        async def attempt() -> bool:
            async with session.put(url) as response:
                if response.status == 201:
                    return True
                if response.status == 409:
                    return False
                if response.status in _RETRYABLE_HTTP_STATUSES:
                    body = await response.text()
                    raise _RetryableYandexDiskError(
                        response.status,
                        f"create_directory status={response.status}: {body[:200]}",
                    )
                response.raise_for_status()
            return False

        return await _run_with_retries("create_directory", attempt)

    async def _get_upload_url(
        self,
        session: aiohttp.ClientSession,
        path: str,
        *,
        overwrite: bool = True,
    ) -> str:
        url = (
            f"{YANDEX_DISK_API_URL}/upload"
            f"?path={quote(path, safe='')}"
            f"&overwrite={str(overwrite).lower()}"
        )

        async def attempt() -> str:
            async with session.get(url) as response:
                if response.status in _RETRYABLE_HTTP_STATUSES:
                    body = await response.text()
                    raise _RetryableYandexDiskError(
                        response.status,
                        f"get_upload_url status={response.status}: {body[:200]}",
                    )
                response.raise_for_status()
                payload = await response.json()
                return payload["href"]

        return await _run_with_retries("get_upload_url", attempt)

    async def _put_upload_content(
        self,
        session: aiohttp.ClientSession,
        upload_url: str,
        content: bytes,
    ) -> None:
        async def attempt() -> None:
            async with session.put(upload_url, data=content) as response:
                if response.status in _RETRYABLE_HTTP_STATUSES:
                    body = await response.text()
                    raise _RetryableYandexDiskError(
                        response.status,
                        f"upload_content status={response.status}: {body[:200]}",
                    )
                response.raise_for_status()

        await _run_with_retries("upload_content", attempt)

    def build_intake_file_path(
        self,
        file_name: str,
        intake_date: date | None = None,
    ) -> str:
        current_date = intake_date or date.today()
        return (
            f"{self.base_path}/{INTAKE_FOLDER}/"
            f"{current_date.isoformat()}/{file_name}"
        )

    def build_case_folder_path(self, case_folder_name: str) -> str:
        return f"{self.base_path}/{CASES_FOLDER}/{case_folder_name}"

    def build_case_file_path(self, case_folder_name: str, file_name: str) -> str:
        return f"{self.build_case_folder_path(case_folder_name)}/{file_name}"

    def build_file_path(self, chat_id: int, message_id: int, file_name: str) -> str:
        return self.build_intake_file_path(file_name)

    def build_customer_intake_file_path(
        self,
        telegram_user_id: int,
        file_name: str,
        intake_date: date | None = None,
    ) -> str:
        current_date = intake_date or date.today()
        return (
            f"{self.base_path}/{CUSTOMERS_FOLDER}/{CUSTOMERS_INTAKE_SUBFOLDER}/"
            f"{current_date.isoformat()}/{telegram_user_id}/{file_name}"
        )

    @staticmethod
    def _normalize_path(path: str) -> str:
        return "/" + path.strip("/")


class _RetryableYandexDiskError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _run_with_retries(operation: str, attempt_factory):
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRY_ATTEMPTS):
        try:
            return await attempt_factory()
        except _RetryableYandexDiskError as exc:
            last_error = exc
            if attempt >= _MAX_RETRY_ATTEMPTS - 1:
                logger.error(
                    "Yandex Disk %s retries exhausted status=%s attempts=%s: %s",
                    operation,
                    exc.status,
                    _MAX_RETRY_ATTEMPTS,
                    exc,
                )
                raise
            delay = _RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "Yandex Disk %s temporary status=%s attempt=%s/%s delay=%ss",
                operation,
                exc.status,
                attempt + 1,
                _MAX_RETRY_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
        except aiohttp.ClientResponseError as exc:
            if exc.status not in _RETRYABLE_HTTP_STATUSES:
                raise
            last_error = exc
            if attempt >= _MAX_RETRY_ATTEMPTS - 1:
                logger.error(
                    "Yandex Disk %s retries exhausted status=%s attempts=%s",
                    operation,
                    exc.status,
                    _MAX_RETRY_ATTEMPTS,
                )
                raise
            delay = _RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "Yandex Disk %s temporary status=%s attempt=%s/%s delay=%ss",
                operation,
                exc.status,
                attempt + 1,
                _MAX_RETRY_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Yandex Disk {operation} retries exhausted")
