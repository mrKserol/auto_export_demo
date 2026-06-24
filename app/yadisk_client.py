from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import aiohttp


YANDEX_DISK_API_URL = "https://cloud-api.yandex.net/v1/disk/resources"
INTAKE_FOLDER = "01_Входящие_Telegram"
CUSTOMERS_FOLDER = "02_Клиенты"
CUSTOMERS_INTAKE_SUBFOLDER = "01_Входящие"
CASES_FOLDER = "03_Сделки"


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

    async def upload_bytes(self, path: str, content: bytes) -> str:
        disk_path = self._normalize_path(path)
        await self._ensure_parent_directory(disk_path)

        async with aiohttp.ClientSession(headers=self._headers) as session:
            upload_url = await self._get_upload_url(session, disk_path)
            async with session.put(upload_url, data=content) as response:
                response.raise_for_status()

        return disk_path

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
        url = f"{YANDEX_DISK_API_URL}?path={quote(self._normalize_path(path), safe='')}"
        async with session.put(url) as response:
            if response.status in (201, 409):
                return
            response.raise_for_status()

    async def _get_upload_url(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> str:
        url = (
            f"{YANDEX_DISK_API_URL}/upload"
            f"?path={quote(path, safe='')}&overwrite=true"
        )
        async with session.get(url) as response:
            response.raise_for_status()
            payload = await response.json()
            return payload["href"]

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

