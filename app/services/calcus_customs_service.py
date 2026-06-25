from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_CALCUS_CUSTOMS_API_URL = "https://calcus.ru/api/v1/Customs"
REQUEST_TIMEOUT_SECONDS = 20
SUPPORTED_CURRENCIES = {"RUB", "USD", "EUR", "CNY", "JPY", "KRW"}


class CalcusValidationError(ValueError):
    pass


class CalcusApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CalcusAuthVariant:
    mode: str
    headers: dict[str, str]
    body_extra: dict[str, str] | None = None
    query_params: dict[str, str] | None = None


@dataclass(frozen=True)
class CalcusCustomsResult:
    success: bool
    payload: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def map_engine_type_to_calcus(eng_type: str | None) -> tuple[int, list[str]]:
    warnings: list[str] = []
    normalized = (eng_type or "").strip().lower().replace("ё", "е")

    mapping = {
        "бензин": 1,
        "бензиновый": 1,
        "gasoline": 1,
        "petrol": 1,
        "дизель": 2,
        "дизельный": 2,
        "diesel": 2,
        "электро": 4,
        "электрический": 4,
        "electric": 4,
        "ev": 4,
        "последовательный гибрид": 5,
        "параллельный гибрид": 6,
        "гибрид": 6,
        "hybrid": 6,
    }

    for key, value in mapping.items():
        if key in normalized:
            return value, warnings

    warnings.append("Тип ДВС не распознан, для Calcus использован бензиновый двигатель.")
    return 1, warnings


def calculate_calcus_age_category(vehicle_year: int, calculation_year: int) -> str:
    age_years = calculation_year - vehicle_year
    if age_years < 3:
        return "0-3"
    if age_years < 5:
        return "3-5"
    if age_years < 7:
        return "5-7"
    return "7-0"


def map_power_unit_to_calcus(power_unit: str | None = None) -> int:
    normalized = (power_unit or "").strip().lower().replace("ё", "е")
    if normalized in {"kw", "квт"}:
        return 2
    return 1


def parse_vehicle_year(year_value: object) -> int:
    text = str(year_value or "").strip()
    if not re.fullmatch(r"\d{4}", text):
        raise CalcusValidationError("Некорректный год автомобиля для Calcus API.")
    return int(text)


def parse_engine_capacity_cc(raw_value: object) -> int:
    text = str(raw_value or "").strip().lower().replace(",", ".")
    if text in {"электро", "ev", "electric"}:
        return 0

    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        raise CalcusValidationError("Некорректный объём двигателя для Calcus API.")

    value = float(digits)
    if value <= 0:
        raise CalcusValidationError("Некорректный объём двигателя для Calcus API.")
    if value < 100:
        return int(value * 1000)
    return int(value)


def normalize_currency(raw_value: str | None) -> tuple[str, list[str]]:
    warnings: list[str] = []
    currency = (raw_value or "").strip().upper()
    if not currency:
        warnings.append("Валюта не указана, для Calcus использован CNY.")
        return "CNY", warnings
    if currency not in SUPPORTED_CURRENCIES:
        raise CalcusValidationError(
            f"Валюта {currency} не поддерживается Calcus API."
        )
    return currency, warnings


def build_calcus_customs_payload(
    specification: dict,
    engine_power: float,
    calculation_year: int | None = None,
    owner: int = 1,
    power_unit: int | None = None,
    price_abroad: float | None = None,
) -> dict[str, Any]:
    if calculation_year is None:
        calculation_year = datetime.now().year

    vehicle_year = parse_vehicle_year(specification.get("year"))
    currency, currency_warnings = normalize_currency(specification.get("price_currency"))
    engine_code, engine_warnings = map_engine_type_to_calcus(specification.get("eng_type"))
    engine_capacity_cc = parse_engine_capacity_cc(specification.get("eng_capacity"))

    if price_abroad is not None:
        price = float(price_abroad)
    else:
        price_text = str(specification.get("price") or "").strip().replace(",", ".")
        if not price_text:
            raise CalcusValidationError("Не указана стоимость автомобиля для Calcus API.")
        try:
            price = float(price_text)
        except ValueError as exc:
            raise CalcusValidationError("Некорректная стоимость автомобиля для Calcus API.") from exc

    if engine_power <= 0:
        raise CalcusValidationError("Мощность двигателя должна быть больше нуля.")

    _ = currency_warnings, engine_warnings

    return {
        "owner": owner,
        "age": calculate_calcus_age_category(vehicle_year, calculation_year),
        "engine": engine_code,
        "power": float(engine_power),
        "power_unit": power_unit if power_unit is not None else 1,
        "value": engine_capacity_cc,
        "price": price,
        "curr": currency,
        "year": calculation_year,
    }


class CalcusCustomsService:
    def __init__(
        self,
        api_key: str | None = None,
        client_id: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else os.getenv("CALCUS_API_KEY") or "").strip()
        self._client_id = (client_id if client_id is not None else os.getenv("CALCUS_CLIENT_ID") or "").strip()
        self._api_url = (
            api_url
            if api_url is not None
            else os.getenv("CALCUS_CUSTOMS_API_URL", DEFAULT_CALCUS_CUSTOMS_API_URL)
        ).strip()

    async def calculate_customs(
        self,
        specification: dict,
        engine_power: float,
        *,
        calculation_year: int | None = None,
        owner: int = 1,
        power_unit: int | None = None,
        price_abroad: float | None = None,
    ) -> CalcusCustomsResult:
        warnings: list[str] = []
        if not self._client_id:
            return CalcusCustomsResult(
                success=False,
                error="CALCUS_CLIENT_ID is not configured",
            )
        if not self._api_key:
            return CalcusCustomsResult(
                success=False,
                error="CALCUS_API_KEY is not configured",
            )

        try:
            payload = build_calcus_customs_payload(
                specification,
                engine_power,
                calculation_year=calculation_year,
                owner=owner,
                power_unit=power_unit,
                price_abroad=price_abroad,
            )
            _, currency_warnings = normalize_currency(specification.get("price_currency"))
            _, engine_warnings = map_engine_type_to_calcus(specification.get("eng_type"))
            warnings.extend(currency_warnings)
            warnings.extend(engine_warnings)
        except CalcusValidationError as error:
            return CalcusCustomsResult(success=False, error=str(error), warnings=warnings)

        try:
            response = await self._post_customs(payload)
        except CalcusApiError as error:
            logger.warning(
                "Calcus customs API failed with status %s: %s",
                error.status_code,
                error,
            )
            return CalcusCustomsResult(success=False, error=str(error), warnings=warnings, payload=payload)
        except Exception:
            logger.exception("Calcus customs API request failed")
            return CalcusCustomsResult(
                success=False,
                error="Calcus API request failed",
                warnings=warnings,
                payload=payload,
            )

        return CalcusCustomsResult(
            success=True,
            payload=payload,
            response=response,
            warnings=warnings,
        )

    async def _post_customs(self, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        last_status: int | None = None
        last_body = ""
        last_auth_mode: str | None = None

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for variant in self._build_auth_variants():
                response, body_text = await self._request_with_auth_variant(
                    session,
                    variant,
                    payload,
                )

                if response.status < 400:
                    logger.info("Calcus customs API succeeded with auth_mode=%s", variant.mode)
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError as exc:
                        raise CalcusApiError("Calcus API returned invalid JSON") from exc

                    if not isinstance(data, dict):
                        raise CalcusApiError("Calcus API returned unexpected response format")
                    return data

                last_status = response.status
                last_body = body_text
                last_auth_mode = variant.mode

                if response.status in {401, 403}:
                    logger.warning(
                        "Calcus customs API auth failed: status=%s auth_mode=%s body=%s",
                        response.status,
                        variant.mode,
                        _safe_short_body(body_text),
                    )
                    continue

                break

        short_error = _short_error_message(last_body, last_status or 401)
        raise CalcusApiError(
            f"{short_error} (auth_mode={last_auth_mode})",
            status_code=last_status,
        )

    def _build_auth_variants(self) -> list[CalcusAuthVariant]:
        # OpenAPI: Api-Client-Id + Api-Key headers (components.securitySchemes).
        json_headers = {"Content-Type": "application/json"}
        return [
            CalcusAuthVariant(
                mode="api-client-id+api-key",
                headers={
                    **json_headers,
                    "Api-Client-Id": self._client_id,
                    "Api-Key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="x-client-id+x-api-key",
                headers={
                    **json_headers,
                    "X-Client-Id": self._client_id,
                    "X-Api-Key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="x-client-id-uppercase+x-api-key",
                headers={
                    **json_headers,
                    "X-Client-ID": self._client_id,
                    "X-API-Key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="client-id+api-key",
                headers={
                    **json_headers,
                    "Client-Id": self._client_id,
                    "Api-Key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="authorization-bearer+client-id",
                headers={
                    **json_headers,
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Client-Id": self._client_id,
                },
            ),
            CalcusAuthVariant(
                mode="body-client_id-api_key",
                headers=json_headers,
                body_extra={
                    "client_id": self._client_id,
                    "api_key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="body-clientId-apiKey",
                headers=json_headers,
                body_extra={
                    "clientId": self._client_id,
                    "apiKey": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="body-client-key",
                headers=json_headers,
                body_extra={
                    "client": self._client_id,
                    "key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="body-client_id-key",
                headers=json_headers,
                body_extra={
                    "client_id": self._client_id,
                    "key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="query-client_id-api_key",
                headers=json_headers,
                query_params={
                    "client_id": self._client_id,
                    "api_key": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="query-clientId-apiKey",
                headers=json_headers,
                query_params={
                    "clientId": self._client_id,
                    "apiKey": self._api_key,
                },
            ),
            CalcusAuthVariant(
                mode="query-client-key",
                headers=json_headers,
                query_params={
                    "client": self._client_id,
                    "key": self._api_key,
                },
            ),
        ]

    async def _request_with_auth_variant(
        self,
        session: aiohttp.ClientSession,
        variant: CalcusAuthVariant,
        payload: dict[str, Any],
    ) -> tuple[aiohttp.ClientResponse, str]:
        request_payload = dict(payload)
        if variant.body_extra:
            request_payload.update(variant.body_extra)

        async with session.post(
            self._api_url,
            json=request_payload,
            headers=variant.headers,
            params=variant.query_params,
        ) as response:
            body_text = await response.text()
            return response, body_text


def extract_customs_fields(calcus_response: dict[str, Any]) -> dict[str, Any]:
    return {
        "customs_sbor": _to_decimal_field(calcus_response.get("sbor")),
        "customs_tax": _to_decimal_field(calcus_response.get("tax")),
        "customs_util": _to_decimal_field(calcus_response.get("util")),
        "customs_nds": _to_decimal_field(calcus_response.get("nds")),
        "customs_excise": _to_decimal_field(calcus_response.get("excise")),
        "customs_total": _to_decimal_field(calcus_response.get("total")),
        "customs_total2": _to_decimal_field(calcus_response.get("total2")),
        "customs_source": "calcus",
        "customs_raw_response": calcus_response,
        "customs_error": None,
    }


def get_customs_payments_rub(estimate_data: dict[str, Any]) -> float:
    total = estimate_data.get("customs_total")
    if total is not None:
        return float(total)
    return 0.0


def get_car_with_customs_rub(estimate_data: dict[str, Any]) -> float:
    total2 = estimate_data.get("customs_total2")
    if total2 is not None and _is_nonzero_amount(total2):
        return float(total2)
    total = estimate_data.get("customs_total")
    if total is not None and _is_nonzero_amount(total):
        return float(total)
    return 0.0


def get_customs_total_rub(estimate_data: dict[str, Any]) -> float:
    return get_car_with_customs_rub(estimate_data)


def _is_nonzero_amount(value: object) -> bool:
    if value is None:
        return False
    return float(value) != 0.0


def _to_decimal_field(value: object) -> float:
    if value is None:
        return 0.0
    return float(value)


def _short_error_message(body_text: str, status_code: int) -> str:
    compact = _safe_short_body(body_text)
    if compact and compact != "empty body":
        return f"Calcus API error {status_code}: {compact}"
    return f"Calcus API error {status_code}"


def _safe_short_body(body_text: str) -> str:
    compact = re.sub(r"\s+", " ", body_text or "").strip()
    if len(compact) > 200:
        compact = compact[:197] + "..."
    return compact or "empty body"
