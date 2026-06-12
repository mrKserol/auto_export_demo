from __future__ import annotations

import re
import unicodedata

from app.database import Database


AUTO_BIND_SCORE = 80
UNCERTAIN_BIND_SCORE = 40
NO_VIN = "NO_VIN"
UNKNOWN = "UNKNOWN"
MAX_FOLDER_NAME_LENGTH = 180


def extract_business_fields(function_response_json: dict) -> dict:
    extracted_data = function_response_json.get("extracted_data") or {}
    customer_full_name = _clean_text(_get_nested(extracted_data, "customer", "full_name"))
    buyer_name = _clean_text(_get_nested(extracted_data, "buyer", "name"))
    passport_series = _clean_text(
        _get_nested(extracted_data, "customer", "passport_series")
    )
    passport_number = _clean_text(
        _get_nested(extracted_data, "customer", "passport_number")
    )
    vin = _normalize_vin(_get_nested(extracted_data, "car", "vin"))

    fields = {
        "document_type": _clean_text(extracted_data.get("document_type")),
        "vin": vin,
        "vin_last4": _get_vin_last4(vin),
        "car_brand": _clean_text(_get_nested(extracted_data, "car", "brand")),
        "car_model": _clean_text(_get_nested(extracted_data, "car", "model")),
        "car_year": _clean_text(_get_nested(extracted_data, "car", "year")),
        "engine_number": _clean_text(_get_nested(extracted_data, "car", "engine_number")),
        "car_color": _clean_text(_get_nested(extracted_data, "car", "color")),
        "customer_name": customer_full_name,
        "customer_surname": _extract_surname(customer_full_name or buyer_name),
        "customer_passport": _join_passport(passport_series, passport_number),
        "passport_series": passport_series,
        "passport_number": passport_number,
        "price_amount": _clean_text(_get_nested(extracted_data, "price", "amount")),
        "price_currency": _clean_text(_get_nested(extracted_data, "price", "currency")),
        "net_weight": _clean_text(_get_nested(extracted_data, "weights", "net_weight")),
        "gross_weight": _clean_text(
            _get_nested(extracted_data, "weights", "gross_weight")
        ),
        "weight_unit": _clean_text(_get_nested(extracted_data, "weights", "weight_unit")),
        "hs_code": _clean_text(_get_nested(extracted_data, "customs", "hs_code")),
        "tn_ved_code": _clean_text(
            _get_nested(extracted_data, "customs", "tn_ved_code")
        ),
        "customs_declaration_number": _clean_text(
            _get_nested(extracted_data, "customs", "declaration_number")
        ),
        "ptd_number": _clean_text(_get_nested(extracted_data, "customs", "ptd_number")),
        "epts_number": _clean_text(
            _get_nested(extracted_data, "vehicle_passport", "epts_number")
        ),
        "epts_status": _clean_text(
            _get_nested(extracted_data, "vehicle_passport", "epts_status")
        ),
        "epts_issue_date": _clean_text(
            _get_nested(extracted_data, "vehicle_passport", "epts_issue_date")
        ),
        "seller_name": _clean_text(_get_nested(extracted_data, "seller", "name")),
        "buyer_name": buyer_name,
        "invoice_number": _clean_text(
            _get_nested(extracted_data, "document_numbers", "invoice_number")
        ),
        "contract_number": _clean_text(
            _get_nested(extracted_data, "document_numbers", "contract_number")
        ),
        "document_declaration_number": _clean_text(
            _get_nested(extracted_data, "document_numbers", "declaration_number")
        ),
        "document_date": _clean_text(_get_nested(extracted_data, "dates", "document_date")),
        "invoice_date": _clean_text(_get_nested(extracted_data, "dates", "invoice_date")),
        "contract_date": _clean_text(_get_nested(extracted_data, "dates", "contract_date")),
        "country_from": _clean_text(_get_nested(extracted_data, "shipment", "from")),
        "country_to": _clean_text(_get_nested(extracted_data, "shipment", "to")),
        "country_of_origin": _clean_text(
            _get_nested(extracted_data, "shipment", "country_of_origin")
        ),
    }
    fields["route"] = _build_route(fields.get("country_from"), fields.get("country_to"))
    return fields


async def save_document_fields(
    database: Database,
    document_id: int,
    case_id: int | None,
    extracted_data: dict,
) -> None:
    await database.save_document_fields(document_id, case_id, extracted_data)


async def find_case_candidates(database: Database, fields: dict) -> list[dict]:
    candidates = await database.find_candidate_cases(fields)
    scored_candidates = []
    for candidate in candidates:
        score = _score_case(candidate, fields)
        candidate_summary = _candidate_summary(candidate)
        candidate_summary["score"] = score
        scored_candidates.append(candidate_summary)
    return sorted(scored_candidates, key=lambda item: item["score"], reverse=True)


async def find_or_create_case(
    database: Database,
    fields: dict,
    case_folder_path_builder=None,
) -> dict:
    candidates = await find_case_candidates(database, fields)
    best_candidate = candidates[0] if candidates else None

    if best_candidate and best_candidate["score"] >= AUTO_BIND_SCORE:
        case = await update_case_from_fields(
            database,
            best_candidate["id"],
            fields,
            existing_case=best_candidate,
        )
        if not case.get("case_folder_name") or not case.get("case_folder_path"):
            case_folder_name = build_case_folder_name(case["id"], case | fields)
            case_folder_path = (
                case_folder_path_builder(case_folder_name)
                if case_folder_path_builder
                else f"03_Сделки/{case_folder_name}"
            )
            case = await database.update_case_identity(
                case["id"],
                case_key=case.get("case_key") or _build_case_key(case["id"], fields),
                folder_name=case_folder_name,
                folder_path=case_folder_path,
            )
        return {
            "case": case,
            "status": "auto_bound",
            "score": best_candidate["score"],
            "candidates": candidates,
            "created": False,
        }

    if best_candidate and best_candidate["score"] >= UNCERTAIN_BIND_SCORE:
        return {
            "case": None,
            "status": "needs_manual_bind",
            "score": best_candidate["score"],
            "candidates": candidates,
            "created": False,
        }

    case = await database.create_case(fields)
    case_folder_name = build_case_folder_name(case["id"], fields)
    case_folder_path = (
        case_folder_path_builder(case_folder_name)
        if case_folder_path_builder
        else f"03_Сделки/{case_folder_name}"
    )
    case_key = _build_case_key(case["id"], fields)
    case = await database.update_case_identity(
        case["id"],
        case_key=case_key,
        folder_name=case_folder_name,
        folder_path=case_folder_path,
    )
    return {
        "case": case,
        "status": "new_case",
        "score": 100 if fields.get("vin") else 0,
        "candidates": [],
        "created": True,
    }


async def update_case_from_fields(
    database: Database,
    case_id: int,
    fields: dict,
    existing_case: dict | None = None,
) -> dict:
    if existing_case:
        conflict_checks = _build_conflict_checks(existing_case, fields)
        if conflict_checks:
            await database.save_case_checks(case_id, conflict_checks)
    return await database.update_case_from_fields(case_id, fields)


def build_case_folder_name(case_id: int, fields_or_case: dict) -> str:
    surname = fields_or_case.get("customer_surname") or UNKNOWN
    brand = fields_or_case.get("car_brand") or UNKNOWN
    model = fields_or_case.get("car_model") or UNKNOWN
    vin_last4 = fields_or_case.get("vin_last4") or _get_vin_last4(
        fields_or_case.get("vin")
    )
    raw_name = f"CASE_{case_id:06d}_{surname}_{brand}_{model}_{vin_last4}"
    return _normalize_folder_name(raw_name)


async def bind_document_to_case(
    database: Database,
    document_id: int,
    case_id: int,
    score: int,
    status: str,
) -> None:
    await database.bind_document_to_case(
        document_id,
        case_id,
        score=score,
        status=status,
    )


async def run_basic_case_checks(database: Database, case_id: int) -> list[dict]:
    checks = [
        await _build_consistency_check(database, case_id, "vin", "vin_consistency"),
        await _build_consistency_check(
            database,
            case_id,
            "contract_number",
            "contract_number_consistency",
        ),
        await _build_presence_check(
            database,
            case_id,
            "epts_presence",
            "epts_number",
            ("эптс",),
        ),
        await _build_presence_check(
            database,
            case_id,
            "invoice_presence",
            "invoice_number",
            ("invoice", "инвойс", "счет-фактура", "счёт-фактура"),
        ),
    ]
    await database.save_case_checks(case_id, checks)
    return checks


def build_case_success_reply(
    *,
    case_data: dict,
    fields: dict,
    pages_processed: object,
) -> str:
    return (
        "✅ Файл обработан и привязан к кейсу\n\n"
        f"Кейс: {_value_or_not_found(case_data.get('case_folder_name'))}\n"
        f"Тип: {_value_or_not_found(fields.get('document_type'))}\n"
        f"VIN: {_value_or_not_found(fields.get('vin'))}\n"
        f"ЭПТС: {_value_or_not_found(fields.get('epts_number'))}\n"
        f"Контракт: {_value_or_not_found(fields.get('contract_number'))}\n"
        f"Инвойс: {_value_or_not_found(fields.get('invoice_number'))}\n"
        f"Страниц: {_value_or_not_found(pages_processed)}"
    )


def build_uncertain_binding_reply(
    *,
    document_id: int,
    original_filename: str,
    fields: dict,
    candidates: list[dict],
) -> str:
    candidate_lines = []
    for index, candidate in enumerate(candidates[:5], start=1):
        candidate_lines.append(
            f"{index}. {_value_or_not_found(candidate.get('case_folder_name'))} — "
            f"VIN {_value_or_not_found(candidate.get('vin'))}, "
            f"контракт {_value_or_not_found(candidate.get('contract_number'))}"
        )

    return (
        "⚠️ Файл обработан, но я не уверен, к какой сделке его привязать.\n\n"
        f"Документ: {original_filename}\n"
        f"Найдено: VIN {_value_or_not_found(fields.get('vin'))}, "
        f"контракт {_value_or_not_found(fields.get('contract_number'))}\n\n"
        "Похожие сделки:\n"
        f"{chr(10).join(candidate_lines) if candidate_lines else 'не найдено'}\n\n"
        "Ответьте:\n"
        f"/bind {document_id} <case_id>"
    )


def _score_case(case: dict, fields: dict) -> int:
    score = 0
    score += _score_exact(case, fields, "vin", 100)
    score += _score_exact(case, fields, "contract_number", 80)
    score += _score_exact(case, fields, "invoice_number", 70)
    score += _score_exact(case, fields, "customer_passport", 70)
    score += _score_exact(case, fields, "epts_number", 70)
    score += _score_exact(case, fields, "customer_surname", 20)
    score += _score_exact(case, fields, "car_brand", 10)
    score += _score_model(case.get("car_model"), fields.get("car_model"))
    score += _score_exact(case, fields, "vin_last4", 30)
    return score


def _score_exact(case: dict, fields: dict, key: str, points: int) -> int:
    case_value = _clean_text(case.get(key))
    field_value = _clean_text(fields.get(key))
    if case_value and field_value and case_value == field_value:
        return points
    return 0


def _score_model(case_model: object, incoming_model: object) -> int:
    case_value = _clean_text(case_model)
    field_value = _clean_text(incoming_model)
    if not case_value or not field_value:
        return 0
    if case_value == field_value or case_value in field_value or field_value in case_value:
        return 10
    return 0


async def _build_consistency_check(
    database: Database,
    case_id: int,
    field_name: str,
    check_code: str,
) -> dict:
    values = await database.get_case_field_values(case_id, field_name)
    if len(values) > 1:
        return {
            "check_code": check_code,
            "severity": "error",
            "status": "error",
            "message": f"Найдены разные значения поля {field_name}.",
            "details_json": {"values": values},
        }
    if len(values) == 1:
        return {
            "check_code": check_code,
            "severity": "info",
            "status": "ok",
            "message": f"Поле {field_name} согласовано.",
            "details_json": {"values": values},
        }
    return {
        "check_code": check_code,
        "severity": "info",
        "status": "not_enough_data",
        "message": f"Недостаточно данных для проверки {field_name}.",
        "details_json": {},
    }


async def _build_presence_check(
    database: Database,
    case_id: int,
    check_code: str,
    field_name: str,
    document_type_markers: tuple[str, ...],
) -> dict:
    values = await database.get_case_field_values(case_id, field_name)
    document_types = await database.get_case_document_types(case_id)
    has_document_type = any(
        marker in document_type.lower()
        for document_type in document_types
        for marker in document_type_markers
    )
    if values or has_document_type:
        return {
            "check_code": check_code,
            "severity": "info",
            "status": "ok",
            "message": f"Данные для {field_name} найдены.",
            "details_json": {"values": values, "document_types": document_types},
        }
    return {
        "check_code": check_code,
        "severity": "info",
        "status": "not_enough_data",
        "message": f"Недостаточно данных для {field_name}.",
        "details_json": {"document_types": document_types},
    }


def _build_conflict_checks(existing_case: dict, fields: dict) -> list[dict]:
    checks = []
    conflict_rules = [
        ("vin", "error", "VIN mismatch"),
        ("contract_number", "warning", "Contract number mismatch"),
        ("invoice_number", "warning", "Invoice number mismatch"),
        ("customer_passport", "error", "Customer passport mismatch"),
        ("car_brand", "warning", "Car brand mismatch"),
        ("car_model", "warning", "Car model mismatch"),
    ]
    for field_name, severity, message in conflict_rules:
        existing_value = _clean_text(existing_case.get(field_name))
        incoming_value = _clean_text(fields.get(field_name))
        if existing_value and incoming_value and existing_value != incoming_value:
            checks.append(
                {
                    "check_code": f"{field_name}_conflict",
                    "severity": severity,
                    "status": "error" if severity == "error" else "warning",
                    "message": message,
                    "details_json": {
                        "existing": existing_value,
                        "incoming": incoming_value,
                    },
                }
            )
    return checks


def _candidate_summary(candidate: dict) -> dict:
    return {
        "id": candidate.get("id"),
        "case_folder_name": candidate.get("case_folder_name"),
        "case_folder_path": candidate.get("case_folder_path"),
        "vin": candidate.get("vin"),
        "vin_last4": candidate.get("vin_last4"),
        "contract_number": candidate.get("contract_number"),
        "invoice_number": candidate.get("invoice_number"),
        "customer_surname": candidate.get("customer_surname"),
        "customer_passport": candidate.get("customer_passport"),
        "car_brand": candidate.get("car_brand"),
        "car_model": candidate.get("car_model"),
        "epts_number": candidate.get("epts_number"),
    }


def _build_case_key(case_id: int, fields: dict) -> str:
    if fields.get("vin"):
        return f"vin:{fields['vin']}"
    if fields.get("contract_number"):
        return f"contract:{fields['contract_number']}"
    return f"case:{case_id:06d}"


def _normalize_folder_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = "".join(
        char if char.isalnum() or char in ("_", "-") else "_"
        for char in normalized
    )
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return (normalized or UNKNOWN)[:MAX_FOLDER_NAME_LENGTH]


def _extract_surname(full_name: str | None) -> str | None:
    if not full_name:
        return None
    token = full_name.strip().split()[0]
    if not token:
        return None
    return token[:1].upper() + token[1:]


def _normalize_vin(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return re.sub(r"\s+", "", text).upper()


def _get_vin_last4(vin: str | None) -> str:
    if vin and len(vin) >= 4:
        return vin[-4:]
    return NO_VIN


def _join_passport(series: str | None, number: str | None) -> str | None:
    parts = [part for part in (series, number) if part]
    return " ".join(parts) if parts else None


def _build_route(country_from: str | None, country_to: str | None) -> str | None:
    if country_from and country_to:
        return f"{country_from} -> {country_to}"
    return country_from or country_to


def _get_nested(payload: dict, *keys: str) -> object:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text or None


def _value_or_not_found(value: object) -> str:
    if value is None or value == "":
        return "не найдено"
    return str(value)
