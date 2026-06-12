from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import aiohttp


YANDEX_DISK_API_URL = "https://cloud-api.yandex.net/v1/disk/resources"


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

    def build_file_path(self, chat_id: int, message_id: int, file_name: str) -> str:
        return f"{self.base_path}/{chat_id}/{file_name}"

    @staticmethod
    def _normalize_path(path: str) -> str:
        return "/" + path.strip("/")

