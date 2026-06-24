from __future__ import annotations

import re
import unicodedata

from app.services.validation_service import (
    normalize_date,
    normalize_department_code,
    normalize_passport,
    normalize_snils,
    normalize_tin,
    validate_registration_address,
)

CUSTOMER_FIELDS = (
    "passport",
    "first_name",
    "last_name",
    "surname",
    "tin",
    "ipain",
    "by_whom_issued",
    "date_issue",
    "registration_address",
    "department_code",
)

_ISSUED_BY_MARKERS = ("кем выдан", "выдан", "уфмс", "гу мвд", "мвд")
_REGISTRATION_MARKERS = (
    "зарегистрирован",
    "зарегистрирована",
    "место жительства",
    "адрес",
    "регистрация",
)
_DATE_MARKERS = ("дата выдачи", "выдан")


def extract_customer_fields_from_response(response_json: dict) -> dict:
    extracted_data = response_json.get("extracted_data") or {}
    fields: dict[str, str | None] = {}

    passport_series = _clean_text(_get_nested(extracted_data, "customer", "passport_series"))
    passport_number = _clean_text(_get_nested(extracted_data, "customer", "passport_number"))
    passport_raw = _clean_text(_get_nested(extracted_data, "customer", "passport"))
    if not passport_raw:
        passport_raw = _join_passport(passport_series, passport_number)
    fields["passport"] = normalize_passport(passport_raw) or normalize_passport(
        _join_passport(passport_series, passport_number)
    )

    full_name = _clean_text(_get_nested(extracted_data, "customer", "full_name"))
    if not full_name:
        full_name = _clean_text(_get_nested(extracted_data, "buyer", "name"))
    if full_name:
        fields.update(_parse_fio(full_name))

    tin_candidates = [
        _get_nested(extracted_data, "customer", "inn"),
        _get_nested(extracted_data, "customer", "tin"),
        _get_nested(extracted_data, "tax", "inn"),
        _get_nested(extracted_data, "tax", "tin"),
        extracted_data.get("inn"),
        extracted_data.get("tin"),
    ]
    for candidate in tin_candidates:
        normalized = normalize_tin(_clean_text(candidate))
        if normalized:
            fields["tin"] = normalized
            break

    snils_candidates = [
        _get_nested(extracted_data, "customer", "snils"),
        _get_nested(extracted_data, "customer", "ipain"),
        extracted_data.get("snils"),
        extracted_data.get("ipain"),
    ]
    for candidate in snils_candidates:
        normalized = normalize_snils(_clean_text(candidate))
        if normalized:
            fields["ipain"] = normalized
            break

    issued_by = _clean_text(_get_nested(extracted_data, "customer", "passport_issued_by"))
    if issued_by:
        fields["by_whom_issued"] = issued_by

    issue_date = normalize_date(
        _clean_text(_get_nested(extracted_data, "customer", "passport_issue_date"))
    )
    if issue_date:
        fields["date_issue"] = issue_date

    address = _clean_text(_get_nested(extracted_data, "customer", "address"))
    if address and validate_registration_address(address):
        fields["registration_address"] = address.strip()

    department_code = normalize_department_code(
        _clean_text(_get_nested(extracted_data, "customer", "department_code"))
    )
    if department_code:
        fields["department_code"] = department_code

    ocr_text = _collect_ocr_text(response_json, extracted_data)
    _extract_from_flat_text(extracted_data, fields, ocr_text)
    return {key: value for key, value in fields.items() if value}


def merge_customer_fields(current: dict, incoming: dict) -> tuple[dict, list[str]]:
    merged = dict(current)
    warnings: list[str] = []

    for key, value in incoming.items():
        if key not in CUSTOMER_FIELDS or not value:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = value
        elif existing != value:
            warnings.append(
                f"Конфликт поля {key}: оставлено «{existing}», проигнорировано «{value}»"
            )

    return merged, warnings


def is_customer_data_complete(data: dict) -> bool:
    return bool(data.get("passport"))


def _collect_ocr_text(response_json: dict, extracted_data: dict) -> str:
    parts: list[str] = []
    for key in ("ocr_text", "ocr_text_preview"):
        value = response_json.get(key) or extracted_data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _extract_from_flat_text(
    extracted_data: dict,
    fields: dict,
    ocr_text: str,
) -> None:
    text_parts: list[str] = []

    def collect_strings(obj: object) -> None:
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            for value in obj.values():
                collect_strings(value)
        elif isinstance(obj, list):
            for item in obj:
                collect_strings(item)

    collect_strings(extracted_data)
    if ocr_text:
        text_parts.append(ocr_text)
    combined = "\n".join(text_parts)

    if not fields.get("passport"):
        passport_match = re.search(r"\b(\d{2})\s*(\d{2})\s*(\d{6})\b", combined)
        if passport_match:
            fields["passport"] = normalize_passport(
                f"{passport_match.group(1)}{passport_match.group(2)}{passport_match.group(3)}"
            )

    if not fields.get("ipain"):
        snils_match = re.search(r"\b(\d{3})[-\s]?(\d{3})[-\s]?(\d{3})[-\s]?(\d{2})\b", combined)
        if snils_match:
            fields["ipain"] = normalize_snils("".join(snils_match.groups()))

    if not fields.get("tin"):
        inn_match = re.search(r"\b(\d{12})\b", combined)
        if inn_match:
            fields["tin"] = normalize_tin(inn_match.group(1))
        else:
            inn_match_10 = re.search(r"\b(\d{10})\b", combined)
            if inn_match_10:
                fields["tin"] = normalize_tin(inn_match_10.group(1))

    if not fields.get("department_code"):
        code = normalize_department_code(combined)
        if code:
            fields["department_code"] = code

    if not fields.get("date_issue"):
        date_value = _extract_date_near_markers(combined, _DATE_MARKERS)
        if date_value:
            fields["date_issue"] = date_value

    if not fields.get("by_whom_issued"):
        issued_by = _extract_issued_by(combined)
        if issued_by:
            fields["by_whom_issued"] = issued_by

    if not fields.get("registration_address"):
        address = _extract_registration_address(combined)
        if address:
            fields["registration_address"] = address


def _extract_date_near_markers(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for marker in markers:
        index = lowered.find(marker)
        if index == -1:
            continue
        fragment = text[index : index + 120]
        normalized = normalize_date(fragment)
        if normalized:
            return normalized
    return normalize_date(text)


def _extract_issued_by(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lowered_text = text.lower()
    for marker in _ISSUED_BY_MARKERS:
        marker_index = lowered_text.find(marker)
        if marker_index == -1:
            continue
        for line in lines:
            line_lower = line.lower()
            if marker in line_lower:
                issued = line
                if len(issued) > 300:
                    issued = issued[:300]
                if len(issued) >= 3:
                    return issued
        fragment = text[marker_index : marker_index + 300].strip()
        fragment = re.sub(r"\s+", " ", fragment)
        if len(fragment) >= 3:
            return fragment[:300]
    return None


def _extract_registration_address(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[tuple[str | None, str]] = []

    for index, line in enumerate(lines):
        line_lower = line.lower()
        if not any(marker in line_lower for marker in _REGISTRATION_MARKERS):
            continue

        address_parts = [line]
        for next_line in lines[index + 1 : index + 4]:
            if _looks_like_address_continuation(next_line):
                address_parts.append(next_line)
            else:
                break

        address = " ".join(address_parts)
        address = re.sub(r"\s+", " ", address).strip()
        if not validate_registration_address(address):
            continue

        date_match = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", address)
        date_value = None
        if date_match:
            date_value = f"{date_match.group(3)}{date_match.group(2)}{date_match.group(1)}"
        candidates.append((date_value, address))

    if candidates:
        dated = [item for item in candidates if item[0] is not None]
        if dated:
            dated.sort(key=lambda item: item[0] or "")
            return dated[-1][1]
        return max(candidates, key=lambda item: len(item[1]))[1]

    long_lines = [line for line in lines if len(line) >= 20 and _looks_like_address_continuation(line)]
    if long_lines:
        best = max(long_lines, key=len)
        if validate_registration_address(best):
            return best
    return None


def _looks_like_address_continuation(line: str) -> bool:
    if len(line) < 5:
        return False
    lowered = line.lower()
    if any(marker in lowered for marker in _REGISTRATION_MARKERS):
        return True
    return bool(re.search(r"\d", line)) and any(
        token in lowered
        for token in ("ул", "дом", "кв", "обл", "р-н", "гор", "пос", "пр", "мкр")
    )


def _parse_fio(full_name: str) -> dict[str, str]:
    tokens = full_name.split()
    result: dict[str, str] = {}
    if len(tokens) >= 1:
        result["last_name"] = tokens[0]
    if len(tokens) >= 2:
        result["first_name"] = tokens[1]
    if len(tokens) >= 3:
        result["surname"] = tokens[2]
    return result


def _join_passport(series: str | None, number: str | None) -> str | None:
    parts = [part for part in (series, number) if part]
    return " ".join(parts) if parts else None


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
