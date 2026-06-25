from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.services.customer_extraction_service import (
    extract_passport_main_fields,
    extract_registration_fields,
    extract_snils_fields,
    extract_tin_fields,
)
from app.services.validation_service import (
    normalize_date,
    normalize_department_code,
    normalize_passport,
    normalize_snils,
    normalize_tin,
)
from app.services.yandex_gpt_service import YandexGPTService, parse_json_response
from app.services.yandex_ocr_service import YandexOCRService


logger = logging.getLogger(__name__)

PASSPORT_MAIN_PROMPT = """Ты извлекаешь данные только с главного разворота паспорта РФ.

OCR текст документа:
---
{ocr_text}
---

Верни только валидный JSON без markdown и без пояснений в таком формате:
{{
  "document_type": "passport_main",
  "passport": null,
  "passport_series": null,
  "passport_number": null,
  "last_name": null,
  "first_name": null,
  "surname": null,
  "by_whom_issued": null,
  "date_issue": null,
  "department_code": null,
  "birth_date": null,
  "birth_place": null,
  "confidence": {{
    "passport": 0,
    "fio": 0,
    "by_whom_issued": 0,
    "date_issue": 0,
    "department_code": 0
  }},
  "warnings": []
}}

Важно про номер паспорта:
- Номер паспорта РФ состоит из 10 цифр: серия 4 цифры + номер 6 цифр.
- На фото паспорта серия и номер часто напечатаны красными цифрами вертикально сбоку страницы.
- Обычно это выглядит как:
  80
  23
  694346
  Значит паспорт: 80 23 694346.
- Не используй нижнюю машинночитаемую строку MRZ.
- MRZ обычно содержит PNRUS, RUS, символы <<, латинские буквы и длинные строки внизу паспорта.
- Не используй даты как номер паспорта.
- Не используй дату рождения.
- Не используй дату выдачи.
- Не используй код подразделения формата 000-000.
- Если не уверен в номере паспорта — верни null.
- Лучше вернуть null, чем неверный номер.

Правила:
- Паспорт храни в формате "80 23 694346".
- Код подразделения в формате "000-000".
- Дата выдачи в формате DD.MM.YYYY.
- Не придумывай данные.
- Если поле не найдено — null.
"""

PASSPORT_REGISTRATION_PROMPT = """Ты извлекаешь данные со страницы регистрации паспорта гражданина РФ.

OCR текст документа:
---
{ocr_text}
---

Верни только валидный JSON без markdown и без пояснений в таком формате:
{{
  "document_type": "passport_registration",
  "passport": null,
  "registrations": [
    {{
      "date": null,
      "region": null,
      "district": null,
      "city": null,
      "street": null,
      "house": null,
      "building": null,
      "apartment": null,
      "raw_address": null
    }}
  ],
  "selected_registration": {{
    "date": null,
    "registration_address": null,
    "raw_address": null
  }},
  "confidence": {{
    "passport": 0,
    "registration_address": 0
  }},
  "warnings": []
}}

Правила:
- На странице может быть несколько штампов "ЗАРЕГИСТРИРОВАН" и "СНЯТ С РЕГИСТРАЦИОННОГО УЧЕТА".
- Выбери последнюю актуальную регистрацию по самой поздней дате.
- Не используй штамп "СНЯТ С РЕГИСТРАЦИОННОГО УЧЕТА" как адрес регистрации.
- Адрес нормализуй в формате: г. Уфа, ул. Ахметова 22 стр. 5 кв. 21
- Если адрес рукописный и не уверен — заполни raw_address, registration_address можно оставить null.
- Паспорт верни в формате "80 06 035956", если найден.
- Не придумывай данные.
"""

SNILS_PROMPT = """Ты извлекаешь данные из документа СНИЛС.

OCR текст документа:
---
{ocr_text}
---

Верни только валидный JSON без markdown и без пояснений в таком формате:
{{
  "document_type": "snils",
  "full_name_raw": null,
  "last_name": null,
  "first_name": null,
  "surname": null,
  "ipain": null,
  "confidence": {{
    "fio": 0,
    "ipain": 0
  }},
  "warnings": []
}}

Правила:
- Извлеки ФИО и номер СНИЛС.
- ФИО в российских документах обычно указано в порядке: Фамилия Имя Отчество.
- last_name = Фамилия.
- first_name = Имя.
- surname = Отчество.
- Не меняй местами имя и фамилию.
- Если ФИО найдено одной строкой, запиши исходную строку в full_name_raw.
- СНИЛС верни строго в формате "123-456-789 00".
- Не придумывай данные.
- Если поле не найдено — null.
"""

TIN_PROMPT = """Ты извлекаешь данные из документа ИНН физического лица.

OCR текст документа:
---
{ocr_text}
---

Верни только валидный JSON без markdown и без пояснений в таком формате:
{{
  "document_type": "tin",
  "full_name_raw": null,
  "last_name": null,
  "first_name": null,
  "surname": null,
  "tin": null,
  "confidence": {{
    "fio": 0,
    "tin": 0
  }},
  "warnings": []
}}

Правила:
- Извлеки ФИО и ИНН.
- ФИО в российских документах обычно указано в порядке: Фамилия Имя Отчество.
- last_name = Фамилия.
- first_name = Имя.
- surname = Отчество.
- Не меняй местами имя и фамилию.
- Если ФИО найдено одной строкой, запиши исходную строку в full_name_raw.
- ИНН верни только цифрами.
- Для физлица обычно 12 цифр.
- Не придумывай данные.
- Если поле не найдено — null.
"""

DOCUMENT_TYPE_GUARD_PROMPT = """Ты определяешь тип документа по OCR-тексту.
OCR текст:
---
{ocr_text}
---
Верни только валидный JSON без markdown:
{{
  "document_type": "passport_main | passport_registration | snils | tin | mixed | unknown",
  "confidence": 0,
  "reason": null,
  "detected_documents": []
}}
Правила:
- passport_main: паспорт РФ, разворот с фотографией и основными данными владельца, есть ФИО, дата рождения, кем выдан, дата выдачи, код подразделения.
- passport_registration: это любая страница паспорта РФ со штампами регистрации или снятия с регистрационного учета.
  Признаки страницы регистрации:
  - крупное слово "ЗАРЕГИСТРИРОВАН"
  - "СНЯТ С РЕГИСТРАЦИОННОГО УЧЕТА"
  - "место жительства"
  - "регистрационного учета"
  - "отделение по району"
  - рукописные строки адреса
  - строки с "рег-н", "р-н", "пункт", "ул", "дом", "корп", "кв"
  - несколько прямоугольных штампов на странице паспорта
  Даже если текст распознан плохо, но видны признаки штампов регистрации паспорта, вернуть passport_registration.
  Не требуй наличие номера паспорта на странице регистрации.
  Не требуй наличие полного адреса.
  Если есть признаки регистрации и нет явных признаков СНИЛС/ИНН, выбирай passport_registration.
  Если документ похож на страницу паспорта со штампами, но OCR не уверен, всё равно верни passport_registration с confidence 0.55-0.7, а не unknown.
- snils: документ СНИЛС, есть номер формата 123-456-789 00 или текст "страховой номер индивидуального лицевого счета".
- tin: ИНН физического лица, есть 12-значный ИНН или текст "свидетельство о постановке на учет".
- mixed: если в OCR-тексте признаки нескольких документов одновременно.
- unknown: если тип определить нельзя.
- Не придумывай данные.
"""

DOCUMENT_TYPE_GUARD_MIN_CONFIDENCE = 0.55


@dataclass(frozen=True)
class CustomerDocumentRecognitionService:
    ocr_service: YandexOCRService
    gpt_service: YandexGPTService

    async def recognize_passport_main(
        self,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
    ) -> dict:
        ocr_text = await self._run_ocr_with_rotation(
            "passport_main",
            file_content,
            mime_type,
            filename=filename,
            score_profile="passport_main",
        )
        guard = await self._detect_document_type_from_ocr(ocr_text)
        if not _is_document_type_allowed(guard, "passport_main"):
            return _build_document_type_mismatch("passport_main", guard)

        gpt_json = await self._run_gpt("passport_main", PASSPORT_MAIN_PROMPT, ocr_text)
        return _postprocess_passport_main(gpt_json, ocr_text)

    async def recognize_passport_registration(
        self,
        file_content: bytes,
        mime_type: str | None,
        expected_passport: str | None = None,
        *,
        filename: str | None = None,
    ) -> dict:
        ocr_text = await self._run_ocr_with_rotation(
            "passport_registration",
            file_content,
            mime_type,
            filename=filename,
            score_profile="registration",
        )
        guard = await self._detect_document_type_from_ocr(ocr_text)
        if not _is_registration_document_type_allowed(guard, ocr_text):
            return _build_document_type_mismatch("passport_registration", guard)

        gpt_json = await self._run_gpt(
            "passport_registration",
            PASSPORT_REGISTRATION_PROMPT,
            ocr_text,
        )
        return _postprocess_passport_registration(
            gpt_json,
            ocr_text,
        )

    async def recognize_snils(
        self,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
    ) -> dict:
        ocr_text = await self._run_ocr("snils", file_content, mime_type, filename=filename)
        guard = await self._detect_document_type_from_ocr(ocr_text)
        if not _is_document_type_allowed(guard, "snils"):
            return _build_document_type_mismatch("snils", guard)

        gpt_json = await self._run_gpt("snils", SNILS_PROMPT, ocr_text)
        return _postprocess_snils(gpt_json, ocr_text)

    async def recognize_tin(
        self,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
    ) -> dict:
        ocr_text = await self._run_ocr("tin", file_content, mime_type, filename=filename)
        guard = await self._detect_document_type_from_ocr(ocr_text)
        if not _is_document_type_allowed(guard, "tin"):
            return _build_document_type_mismatch("tin", guard)

        gpt_json = await self._run_gpt("tin", TIN_PROMPT, ocr_text)
        return _postprocess_tin(gpt_json, ocr_text)

    async def detect_document_type(
        self,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
        ocr_text: str | None = None,
        use_registration_rotation: bool = False,
    ) -> dict:
        if ocr_text is None:
            if use_registration_rotation:
                ocr_text = await self._run_ocr_with_rotation(
                    "document_type_guard",
                    file_content,
                    mime_type,
                    filename=filename,
                )
            else:
                ocr_text = await self._run_ocr(
                    "document_type_guard",
                    file_content,
                    mime_type,
                    filename=filename,
                )
        return await self._detect_document_type_from_ocr(ocr_text)

    async def _detect_document_type_from_ocr(self, ocr_text: str) -> dict:
        gpt_json = await self._run_gpt(
            "document_type_guard",
            DOCUMENT_TYPE_GUARD_PROMPT,
            ocr_text,
        )
        document_type = _clean_text(gpt_json.get("document_type")) or "unknown"
        confidence_raw = gpt_json.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0

        if confidence < DOCUMENT_TYPE_GUARD_MIN_CONFIDENCE:
            document_type = "unknown"

        detected_documents = gpt_json.get("detected_documents")
        if not isinstance(detected_documents, list):
            detected_documents = []

        return {
            "document_type": document_type,
            "confidence": confidence,
            "reason": _clean_text(gpt_json.get("reason")),
            "detected_documents": detected_documents,
        }

    async def _run_ocr_with_rotation(
        self,
        document_type: str,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
        score_profile: str = "registration",
    ) -> str:
        try:
            ocr_text = await self.ocr_service.recognize_text_with_rotation_candidates(
                file_content,
                mime_type,
                filename=filename,
                score_profile=score_profile,
            )
        except AttributeError:
            ocr_text = await self._run_ocr(
                document_type,
                file_content,
                mime_type,
                filename=filename,
            )
        except Exception:
            logger.exception("OCR with rotation failed for document_type=%s", document_type)
            raise

        if not ocr_text:
            logger.warning(
                "OCR with rotation returned empty text for document_type=%s",
                document_type,
            )
        else:
            logger.info(
                "OCR with rotation completed for document_type=%s, text_length=%s",
                document_type,
                len(ocr_text),
            )
        return ocr_text

    async def _run_ocr(
        self,
        document_type: str,
        file_content: bytes,
        mime_type: str | None,
        *,
        filename: str | None = None,
    ) -> str:
        try:
            ocr_text = await self.ocr_service.recognize_text(
                file_content,
                mime_type,
                filename=filename,
            )
        except Exception:
            logger.exception("OCR failed for document_type=%s", document_type)
            raise

        if not ocr_text:
            logger.warning("OCR returned empty text for document_type=%s", document_type)
        else:
            logger.info(
                "OCR completed for document_type=%s, text_length=%s",
                document_type,
                len(ocr_text),
            )
        return ocr_text

    async def _run_gpt(self, document_type: str, prompt_template: str, ocr_text: str) -> dict:
        prompt = prompt_template.format(ocr_text=ocr_text)
        try:
            raw_response = await self.gpt_service.complete(prompt)
        except Exception:
            logger.exception("GPT failed for document_type=%s", document_type)
            raise

        parsed = parse_json_response(raw_response)
        if parsed.get("parse_error"):
            logger.warning("GPT JSON parse failed for document_type=%s", document_type)
        else:
            logger.info("GPT completed for document_type=%s", document_type)
        return parsed


def _is_document_type_allowed(guard: dict, expected_document_type: str) -> bool:
    document_type = guard.get("document_type")
    if document_type in {"mixed", "unknown"}:
        return False
    return document_type == expected_document_type


def _is_registration_document_type_allowed(guard: dict, ocr_text: str) -> bool:
    detected_type = guard.get("document_type")

    if detected_type in {"snils", "tin", "passport_main"}:
        return False

    if detected_type == "passport_registration":
        return True

    if detected_type == "unknown":
        return _looks_like_registration_page(ocr_text)

    if detected_type == "mixed":
        detected_types = _extract_detected_document_types(guard.get("detected_documents"))
        has_registration = "passport_registration" in detected_types
        has_snils_or_tin = "snils" in detected_types or "tin" in detected_types
        if has_snils_or_tin:
            return False
        if has_registration:
            return True
        return _looks_like_registration_page(ocr_text)

    return _looks_like_registration_page(ocr_text)


def _extract_detected_document_types(detected_documents: object) -> set[str]:
    if not isinstance(detected_documents, list):
        return set()

    types: set[str] = set()
    for item in detected_documents:
        if isinstance(item, str):
            normalized = item.strip().lower()
            if normalized:
                types.add(normalized)
            continue
        if isinstance(item, dict):
            doc_type = item.get("document_type") or item.get("type")
            if doc_type:
                types.add(str(doc_type).strip().lower())
    return types


def _looks_like_registration_page(ocr_text: str) -> bool:
    text = (ocr_text or "").upper().replace("Ё", "Е")

    strong_markers = [
        "ЗАРЕГИСТРИРОВАН",
        "РЕГИСТРИРОВАН",
        "СНЯТ С РЕГИСТРАЦИОННОГО УЧЕТА",
        "РЕГИСТРАЦИОННОГО УЧЕТА",
        "МЕСТО ЖИТЕЛЬСТВА",
    ]

    weak_markers = [
        "ОТДЕЛЕНИЕ",
        "РАЙОН",
        "УЛ",
        "ДОМ",
        "КОРП",
        "КВ",
        "ПОДПИСЬ",
        "ФАМИЛИЯ",
    ]

    if any(marker in text for marker in strong_markers):
        return True

    weak_count = sum(1 for marker in weak_markers if marker in text)
    return weak_count >= 3


def _build_document_type_mismatch(expected_document_type: str, guard: dict) -> dict:
    return {
        "document_type_mismatch": True,
        "expected_document_type": expected_document_type,
        "detected_document_type": guard.get("document_type"),
        "document_type_reason": guard.get("reason"),
    }


def _postprocess_passport_main(gpt_json: dict, ocr_text: str) -> dict:
    text_without_mrz = _remove_mrz_lines(ocr_text)

    passport = _validate_passport_candidate(gpt_json.get("passport"), ocr_text)

    if not passport:
        passport = _validate_passport_candidate(
            _extract_passport_side_number_simple(text_without_mrz),
            ocr_text,
        )

    if not passport:
        series = _clean_text(gpt_json.get("passport_series"))
        number = _clean_text(gpt_json.get("passport_number"))
        if series and number:
            passport = _validate_passport_candidate(f"{series}{number}", ocr_text)

    fields = {
        "passport": passport,
        "first_name": _clean_text(gpt_json.get("first_name")),
        "last_name": _clean_text(gpt_json.get("last_name")),
        "surname": _clean_text(gpt_json.get("surname")),
        "by_whom_issued": _clean_text(gpt_json.get("by_whom_issued")),
        "date_issue": normalize_date(_clean_text(gpt_json.get("date_issue"))),
        "department_code": normalize_department_code(_clean_text(gpt_json.get("department_code"))),
    }

    fallback = extract_passport_main_fields({"ocr_text": text_without_mrz})
    for key, value in fallback.items():
        if key == "passport":
            continue
        if value and not fields.get(key):
            fields[key] = value

    if not fields["department_code"]:
        code_match = re.search(r"\b\d{3}-\d{3}\b", ocr_text)
        if code_match:
            fields["department_code"] = normalize_department_code(code_match.group(0))

    if not fields["date_issue"]:
        fields["date_issue"] = normalize_date(ocr_text)

    if not fields.get("passport"):
        logger.warning("Passport number not found after simplified extraction")

    return {key: value for key, value in fields.items() if value}


def _remove_mrz_lines(ocr_text: str) -> str:
    lines: list[str] = []
    for line in (ocr_text or "").splitlines():
        upper = line.upper()
        if "<<" in upper:
            continue
        if "PNRUS" in upper:
            continue
        if re.search(r"[A-Z]{3,}", upper) and re.search(r"\d{6,}", upper):
            continue
        if "RUS" in upper and re.search(r"\d{6,}", upper):
            continue
        lines.append(line)
    return "\n".join(lines)


def _is_likely_mrz_passport_candidate(passport: str | None, ocr_text: str) -> bool:
    normalized = re.sub(r"\D", "", passport or "")
    if len(normalized) != 10:
        return False

    for line in (ocr_text or "").splitlines():
        compact_line = re.sub(r"\D", "", line)
        upper = line.upper()
        if normalized in compact_line and (
            "<<" in upper
            or "PNRUS" in upper
            or "RUS" in upper
            or re.search(r"[A-Z]{3,}", upper)
        ):
            return True

    return False


def _contains_date_like_sequence(digits: str) -> bool:
    if len(digits) != 10:
        return False

    def is_ddmmyyyy(eight_digits: str) -> bool:
        if len(eight_digits) != 8:
            return False
        try:
            day = int(eight_digits[0:2])
            month = int(eight_digits[2:4])
            year = int(eight_digits[4:8])
        except ValueError:
            return False
        return 1 <= day <= 31 and 1 <= month <= 12 and 1900 <= year <= 2099

    return is_ddmmyyyy(digits[:8]) or is_ddmmyyyy(digits[2:10])


def _validate_passport_candidate(candidate: str | None, ocr_text: str) -> str | None:
    digits = re.sub(r"\D", "", candidate or "")
    if len(digits) != 10:
        return None

    if _is_likely_mrz_passport_candidate(digits, ocr_text):
        return None

    if _contains_date_like_sequence(digits):
        return None

    for code_match in re.finditer(r"\b(\d{3})-(\d{3})\b", ocr_text or ""):
        if digits == "".join(code_match.groups()):
            return None

    return normalize_passport(digits)


def _extract_passport_side_number_simple(ocr_text: str) -> str | None:
    text = _remove_mrz_lines(ocr_text)
    tokens: list[str] = []

    for line in text.splitlines():
        upper = line.upper()
        if "ДАТА" in upper:
            continue
        if "РОЖД" in upper:
            continue
        if "ВЫДАЧ" in upper:
            continue
        if "КОД" in upper:
            continue
        if "ПОДРАЗДЕЛ" in upper:
            continue
        if re.search(r"\d{3}-\d{3}", line):
            continue

        tokens.extend(re.findall(r"\d+", line))

    for index in range(len(tokens) - 2):
        if (
            re.fullmatch(r"\d{2}", tokens[index])
            and re.fullmatch(r"\d{2}", tokens[index + 1])
            and re.fullmatch(r"\d{6}", tokens[index + 2])
        ):
            candidate = tokens[index] + tokens[index + 1] + tokens[index + 2]
            return normalize_passport(candidate)

    return None


def _postprocess_passport_registration(
    gpt_json: dict,
    ocr_text: str,
) -> dict:
    selected = gpt_json.get("selected_registration") or {}
    address = _clean_text(selected.get("registration_address"))
    if not address:
        address = _clean_text(selected.get("raw_address"))

    if not address:
        address = _build_registration_address_from_gpt(gpt_json)

    passport = _normalize_passport_value(gpt_json.get("passport"))

    fallback = extract_registration_fields({"ocr_text": ocr_text})
    if not passport:
        passport = fallback.get("passport")
    if not address:
        address = fallback.get("registration_address")

    if not passport:
        passport_match = re.search(r"\b(\d{2})\s*(\d{2})\s*(\d{6})\b", ocr_text)
        if passport_match:
            passport = normalize_passport(
                f"{passport_match.group(1)}{passport_match.group(2)}{passport_match.group(3)}"
            )

    if not address:
        address = _extract_registration_address_fallback(ocr_text)

    result: dict[str, str] = {}
    if passport:
        result["passport"] = passport
    if address:
        result["registration_address"] = address
    return result


def _postprocess_snils(gpt_json: dict, ocr_text: str) -> dict:
    fields = {
        "full_name_raw": _clean_text(gpt_json.get("full_name_raw")),
        "first_name": _clean_text(gpt_json.get("first_name")),
        "last_name": _clean_text(gpt_json.get("last_name")),
        "surname": _clean_text(gpt_json.get("surname")),
        "ipain": normalize_snils(_clean_text(gpt_json.get("ipain"))),
    }
    _apply_full_name_raw(fields)

    fallback = extract_snils_fields({"ocr_text": ocr_text})
    for key, value in fallback.items():
        if value and not fields.get(key):
            fields[key] = value

    if not fields.get("ipain"):
        snils_match = re.search(
            r"\b(\d{3})[-\s]?(\d{3})[-\s]?(\d{3})[-\s]?(\d{2})\b",
            ocr_text,
        )
        if snils_match:
            fields["ipain"] = normalize_snils(
                "".join(snils_match.groups())
            )

    return {key: value for key, value in fields.items() if value}


def _postprocess_tin(gpt_json: dict, ocr_text: str) -> dict:
    fields = {
        "full_name_raw": _clean_text(gpt_json.get("full_name_raw")),
        "first_name": _clean_text(gpt_json.get("first_name")),
        "last_name": _clean_text(gpt_json.get("last_name")),
        "surname": _clean_text(gpt_json.get("surname")),
        "tin": normalize_tin(_clean_text(gpt_json.get("tin"))),
    }
    _apply_full_name_raw(fields)

    fallback = extract_tin_fields({"ocr_text": ocr_text})
    for key, value in fallback.items():
        if value and not fields.get(key):
            fields[key] = value

    if not fields.get("tin"):
        inn_match = re.search(r"\b(\d{12})\b", ocr_text)
        if inn_match:
            fields["tin"] = normalize_tin(inn_match.group(1))
        else:
            inn_match_10 = re.search(r"\b(\d{10})\b", ocr_text)
            if inn_match_10:
                fields["tin"] = normalize_tin(inn_match_10.group(1))

    return {key: value for key, value in fields.items() if value}


def _apply_full_name_raw(fields: dict) -> None:
    full_name_raw = fields.get("full_name_raw")
    has_structured = any(fields.get(key) for key in ("last_name", "first_name", "surname"))
    if has_structured or not full_name_raw:
        return

    tokens = full_name_raw.split()
    if len(tokens) >= 1:
        fields["last_name"] = tokens[0]
    if len(tokens) >= 2:
        fields["first_name"] = tokens[1]
    if len(tokens) >= 3:
        fields["surname"] = tokens[2]


def _build_registration_address_from_gpt(gpt_json: dict) -> str | None:
    registrations = gpt_json.get("registrations") or []
    if not isinstance(registrations, list):
        return None

    best_entry = None
    best_date = None
    for entry in registrations:
        if not isinstance(entry, dict):
            continue
        if _looks_like_deregistration(entry):
            continue
        entry_date = normalize_date(_clean_text(entry.get("date")))
        if entry_date and (best_date is None or entry_date > best_date):
            best_date = entry_date
            best_entry = entry

    if best_entry is None and registrations:
        best_entry = registrations[-1] if isinstance(registrations[-1], dict) else None

    if not best_entry:
        return None

    normalized = _clean_text(best_entry.get("registration_address"))
    if normalized:
        return normalized

    raw_address = _clean_text(best_entry.get("raw_address"))
    if raw_address:
        return raw_address

    parts: list[str] = []
    city = _clean_text(best_entry.get("city"))
    street = _clean_text(best_entry.get("street"))
    house = _clean_text(best_entry.get("house"))
    building = _clean_text(best_entry.get("building"))
    apartment = _clean_text(best_entry.get("apartment"))

    if city:
        parts.append(city if city.startswith("г.") else f"г. {city}")
    if street:
        parts.append(street if street.startswith("ул.") else f"ул. {street}")
    if house:
        parts.append(f"д. {house}" if not house.startswith("д.") else house)
    if building:
        parts.append(
            f"стр. {building}" if not building.startswith("стр.") else building
        )
    if apartment:
        parts.append(f"кв. {apartment}" if not apartment.startswith("кв.") else apartment)

    return ", ".join(parts) if parts else None


def _looks_like_deregistration(entry: dict) -> bool:
    raw = " ".join(
        str(entry.get(key) or "")
        for key in ("raw_address", "region", "district", "city", "street")
    ).upper()
    return "СНЯТ" in raw and "РЕГИСТР" in raw


def _extract_registration_address_fallback(ocr_text: str) -> str | None:
    upper_text = ocr_text.upper()
    marker_index = -1
    for marker in ("ЗАРЕГИСТРИРОВАН", "РЕГ."):
        index = upper_text.rfind(marker)
        if index > marker_index:
            marker_index = index

    if marker_index == -1:
        return None

    snippet = ocr_text[marker_index : marker_index + 400]
    lines = [line.strip(" ,.;") for line in snippet.splitlines() if line.strip()]
    if not lines:
        return None

    collected: list[str] = []
    for line in lines[1:6]:
        upper_line = line.upper()
        if "СНЯТ" in upper_line and "РЕГИСТР" in upper_line:
            break
        if any(token in upper_line for token in ("УЛ.", "ДОМ", "КОРП.", "КВ.", "Г.")):
            collected.append(line)

    if collected:
        return ", ".join(collected)
    if len(lines) > 1:
        return lines[1]
    return None


def _normalize_passport_value(value: object) -> str | None:
    return normalize_passport(_clean_text(value))


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
