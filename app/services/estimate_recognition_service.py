from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.services.yandex_ocr_service import YandexOCRService
from app.services.yandex_gpt_service import YandexGPTService, parse_json_response

logger = logging.getLogger(__name__)


@dataclass
class EstimateRecognitionResult:
    data: dict[str, Any]
    warnings: list[str]


class EstimateRecognitionService:
    def __init__(self, *, ocr_service: YandexOCRService, gpt_service: YandexGPTService) -> None:
        self.ocr = ocr_service
        self.gpt = gpt_service

    async def recognize(
        self,
        *,
        file_bytes: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> EstimateRecognitionResult:
        # Only implement image flow for now (jpg/png)
        if mime_type not in ("image/jpeg", "image/jpg", "image/png"):
            raise RuntimeError("Unsupported mime for recognition in this version")

        # OCR
        text = await self.ocr.recognize_text(file_bytes, mime_type=mime_type, filename=filename)

        # Build prompt for GPT to extract JSON
        prompt = (
            "You are given text extracted by OCR from an estimate (invoice-like).\n"
            "Extract the following fields into JSON with these keys:\n"
            "engine_power, exchange_rate, price, price_currency, inspect_transport_price, bank_commission, "
            "transit_declaration_price, insurance_shipment, customs_total, custom_clearing, contractor_comission, total_rub\n"
            "Numbers should be plain numbers (dot as decimal). If not found, use null.\n"
            "Return ONLY a JSON object.\n\n"
            "Text:\n" + text
        )

        raw = await self.gpt.complete(prompt)
        parsed = parse_json_response(raw)
        if parsed.get("parse_error"):
            logger.warning("EstimateRecognition: GPT returned unparsable JSON")
            # fallback: try to find numbers heuristically (not implemented)
            return EstimateRecognitionResult(data={}, warnings=["parse_error"])

        # Normalize keys: ensure keys exist
        keys = [
            "engine_power",
            "exchange_rate",
            "price",
            "price_currency",
            "inspect_transport_price",
            "bank_commission",
            "transit_declaration_price",
            "insurance_shipment",
            "customs_total",
            "custom_clearing",
            "contractor_comission",
            "total_rub",
        ]
        out = {}
        for k in keys:
            out[k] = parsed.get(k)

        warnings = []
        return EstimateRecognitionResult(data=out, warnings=warnings)

