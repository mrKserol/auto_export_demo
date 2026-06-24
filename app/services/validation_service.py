from __future__ import annotations

import re


def normalize_passport(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return None
    return f"{digits[:2]} {digits[2:4]} {digits[4:]}"


def normalize_snils(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11:
        return None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]} {digits[9:]}"


def normalize_tin(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) not in (10, 12):
        return None
    return digits


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value.strip())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return f"+{digits}"


def validate_email(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text))


def validate_name(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    return 1 <= len(text) <= 80
