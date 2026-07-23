from __future__ import annotations

from ayum.translit import to_latin

_NAME_TO_TRANSLIT = (
    ("first_name", "first_name_translit"),
    ("last_name", "last_name_translit"),
    ("surname", "surname_translit"),
)


def transliterate_russian_name(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    transliterated = to_latin(text).strip()
    return transliterated or None


def apply_name_transliteration(fields: dict) -> dict:
    result = dict(fields)
    for source_key, target_key in _NAME_TO_TRANSLIT:
        result[target_key] = transliterate_russian_name(result.get(source_key))
    return result


def translit_field_for_name(field_name: str) -> str | None:
    for source_key, target_key in _NAME_TO_TRANSLIT:
        if field_name == source_key:
            return target_key
    return None
