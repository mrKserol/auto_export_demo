from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiohttp


logger = logging.getLogger(__name__)

GPT_ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


@dataclass(frozen=True)
class YandexGPTService:
    api_key: str
    folder_id: str

    @property
    def model_uri(self) -> str:
        return f"gpt://{self.folder_id}/yandexgpt-lite/latest"

    async def complete(self, prompt: str) -> str:
        payload = {
            "modelUri": self.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": 0.1,
                "maxTokens": 2000,
            },
            "messages": [
                {
                    "role": "user",
                    "text": prompt,
                }
            ],
        }
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(GPT_ENDPOINT, json=payload, headers=headers) as response:
                response_text = await response.text()
                if response.status >= 400:
                    logger.error(
                        "YandexGPT failed with status %s: %s",
                        response.status,
                        _short_error(response_text),
                    )
                    raise RuntimeError(
                        f"YandexGPT error {response.status}: {_short_error(response_text)}"
                    )

                try:
                    response_json = json.loads(response_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("YandexGPT returned invalid JSON") from exc

                return _extract_completion_text(response_json)


def parse_json_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return {"parse_error": True, "raw_text": raw_text}


def _extract_completion_text(response_json: dict) -> str:
    result = response_json.get("result") or {}
    alternatives = result.get("alternatives") or []
    if not alternatives:
        return ""
    message = alternatives[0].get("message") or {}
    return str(message.get("text") or "").strip()


def _short_error(error_body: str, limit: int = 200) -> str:
    compact = re.sub(r"\s+", " ", error_body).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
