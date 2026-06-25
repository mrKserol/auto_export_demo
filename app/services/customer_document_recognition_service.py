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

PASSPORT_MAIN_PROMPT = """Ты извлекаешь данные из паспорта гражданина РФ, страница 2-3 (разворот с фотографией).

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

Правила:
- Извлекай только данные главной страницы паспорта.
- Паспорт храни в формате "80 06 035956".
- Серия паспорта — 4 цифры, номер — 6 цифр.
- В паспорте РФ серия и номер часто напечатаны красными цифрами вертикально справа на обеих страницах.
- Серия состоит из 4 цифр, часто отображается как две пары: 80 и 23.
- Номер состоит из 6 цифр, например 694346.
- Если OCR видит рядом или построчно три числовых блока 80, 23, 694346, это может быть паспорт: 80 23 694346.
- Не путай номер паспорта с ИНН, СНИЛС, датой рождения, датой выдачи или кодом подразделения.
- Код подразделения имеет формат 000-000, это НЕ номер паспорта.
- Дата выдачи имеет формат DD.MM.YYYY, это НЕ номер паспорта.
- Если на правом краю страницы есть повторяющиеся красные цифры, используй их как источник серии и номера паспорта.
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
        ocr_text = await self._run_ocr(
            "passport_main",
            file_content,
            mime_type,
            filename=filename,
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
    ) -> str:
        try:
            ocr_text = await self.ocr_service.recognize_text_with_rotation_candidates(
                file_content,
                mime_type,
                filename=filename,
                score_profile="registration",
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
    fields = {
        "passport": _normalize_passport_value(gpt_json.get("passport")),
        "first_name": _clean_text(gpt_json.get("first_name")),
        "last_name": _clean_text(gpt_json.get("last_name")),
        "surname": _clean_text(gpt_json.get("surname")),
        "by_whom_issued": _clean_text(gpt_json.get("by_whom_issued")),
        "date_issue": normalize_date(_clean_text(gpt_json.get("date_issue"))),
        "department_code": normalize_department_code(_clean_text(gpt_json.get("department_code"))),
    }

    if not fields["passport"]:
        series = _clean_text(gpt_json.get("passport_series"))
        number = _clean_text(gpt_json.get("passport_number"))
        if series and number:
            fields["passport"] = normalize_passport(f"{series}{number}")

    fallback = extract_passport_main_fields({"ocr_text": ocr_text})
    for key, value in fallback.items():
        if value and not fields.get(key):
            fields[key] = value

    if not fields["passport"]:
        fields["passport"] = _extract_passport_from_vertical_or_side_numbers(ocr_text)

    if not fields["passport"]:
        passport_match = re.search(r"\b(\d{2})\s*(\d{2})\s*(\d{6})\b", ocr_text)
        if passport_match:
            fields["passport"] = normalize_passport(
                f"{passport_match.group(1)}{passport_match.group(2)}{passport_match.group(3)}"
            )

    if not fields["passport"]:
        numeric_tokens = _collect_numeric_tokens_from_ocr(ocr_text)
        logger.warning(
            "Passport number not found. digit_tokens=%s",
            _mask_digit_tokens_for_log(numeric_tokens),
        )

    if not fields["department_code"]:
        code_match = re.search(r"\b\d{3}-\d{3}\b", ocr_text)
        if code_match:
            fields["department_code"] = normalize_department_code(code_match.group(0))

    if not fields["date_issue"]:
        fields["date_issue"] = normalize_date(ocr_text)

    return {key: value for key, value in fields.items() if value}


_PASSPORT_CONTEXT_KEYWORDS = (
    "РОССИЙСКАЯ ФЕДЕРАЦИЯ",
    "ПАСПОРТ",
    "КОД ПОДРАЗДЕЛЕНИЯ",
    "ДАТА ВЫДАЧИ",
)

_EXCLUDED_LINE_KEYWORDS = (
    "ВЫДАЧ",
    "РОЖДЕН",
    "КОД",
    "ПОДРАЗДЕЛ",
    "ИНН",
    "СНИЛС",
    "СТРАХОВ",
)


def _extract_passport_from_vertical_or_side_numbers(ocr_text: str) -> str | None:
    """
    Ищет серию и номер паспорта РФ, если OCR прочитал вертикальный номер
    отдельными строками, например:
    80
    23
    694346
    """
    if not ocr_text:
        return None

    token_entries = _collect_numeric_token_entries_from_ocr(ocr_text)
    if not token_entries:
        return None

    candidates: list[tuple[str, int, int]] = []
    seen_digits: set[str] = set()

    def add_candidate(digits10: str, line_idx: int) -> None:
        normalized = normalize_passport(digits10)
        if not normalized or digits10 in seen_digits:
            return
        if _is_excluded_passport_digits(digits10, ocr_text):
            return
        seen_digits.add(digits10)
        candidates.append((digits10, line_idx, 0))

    for line_idx, token in token_entries:
        if len(token) == 10:
            add_candidate(token, line_idx)

    tokens_only = [token for _, token in token_entries]
    line_indices = [line_idx for line_idx, _ in token_entries]
    token_count = len(tokens_only)
    for index in range(token_count):
        t0 = tokens_only[index]
        line_idx = line_indices[index]

        if index + 2 < token_count:
            t1, t2 = tokens_only[index + 1], tokens_only[index + 2]
            if len(t0) == 2 and len(t1) == 2 and len(t2) == 6:
                add_candidate(f"{t0}{t1}{t2}", line_idx)
            if len(t0) == 4 and len(t1) == 6:
                add_candidate(f"{t0}{t1}", line_idx)

        if index + 3 < token_count:
            t1, t2, t3 = tokens_only[index + 1], tokens_only[index + 2], tokens_only[index + 3]
            if len(t0) == 2 and len(t1) == 2 and len(t2) == 2 and len(t3) == 4:
                add_candidate(f"{t0}{t1}{t2}{t3}", line_idx)

    lines = ocr_text.splitlines()
    for line_idx, line in enumerate(lines):
        upper_line = line.upper().replace("Ё", "Е")
        if any(keyword in upper_line for keyword in _EXCLUDED_LINE_KEYWORDS):
            continue
        for match in re.finditer(r"(?<!\d)(\d{10})(?!\d)", line):
            add_candidate(match.group(1), line_idx)

    if not candidates:
        return None

    occurrence_counts: dict[str, int] = {}
    digits_only_ocr = re.sub(r"\D", "", ocr_text)
    for digits10, _, _ in candidates:
        occurrence_counts[digits10] = digits_only_ocr.count(digits10)

    scored: list[tuple[int, str]] = []
    for digits10, line_idx, _ in candidates:
        score = _score_passport_candidate(
            digits10,
            ocr_text,
            line_idx=line_idx,
            occurrence_count=occurrence_counts.get(digits10, 1),
        )
        if score > 0:
            scored.append((score, digits10))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    return normalize_passport(scored[0][1])


def _collect_numeric_token_entries_from_ocr(ocr_text: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for line_idx, line in enumerate(ocr_text.splitlines()):
        for token in re.findall(r"\d+", line):
            if len(token) in {2, 4, 6, 10}:
                entries.append((line_idx, token))
    return entries


def _collect_numeric_tokens_from_ocr(ocr_text: str) -> list[str]:
    return [token for _, token in _collect_numeric_token_entries_from_ocr(ocr_text)]


def _mask_digit_tokens_for_log(tokens: list[str]) -> list[str]:
    masked: list[str] = []
    for token in tokens:
        if len(token) <= 2:
            masked.append(token)
        elif len(token) == 6:
            masked.append("******")
        else:
            masked.append("*" * len(token))
    return masked


def _is_excluded_passport_digits(digits10: str, ocr_text: str) -> bool:
    if len(digits10) != 10:
        return True

    for date_match in re.finditer(r"\b(\d{2})[./](\d{2})[./](\d{4})\b", ocr_text):
        date_digits = "".join(date_match.groups())
        if digits10 == date_digits:
            return True

    for code_match in re.finditer(r"\b(\d{3})-(\d{3})\b", ocr_text):
        code_digits = "".join(code_match.groups())
        if digits10 == code_digits:
            return True

    for inn_match in re.finditer(r"(?<!\d)(\d{12})(?!\d)", ocr_text):
        if digits10 in inn_match.group(1):
            return True

    for snils_match in re.finditer(
        r"\b(\d{3})[-\s]?(\d{3})[-\s]?(\d{3})[-\s]?(\d{2})\b",
        ocr_text,
    ):
        snils_digits = "".join(snils_match.groups())
        if len(snils_digits) == 11 and digits10 in snils_digits:
            return True

    position = ocr_text.find(digits10)
    if position != -1:
        context = ocr_text[max(0, position - 40) : position + len(digits10) + 40]
        context_upper = context.upper().replace("Ё", "Е")
        if any(keyword in context_upper for keyword in ("ИНН", "СНИЛС", "СТРАХОВ")):
            return True

    return False


def _score_passport_candidate(
    digits10: str,
    ocr_text: str,
    *,
    line_idx: int,
    occurrence_count: int,
) -> int:
    if len(digits10) != 10:
        return -100

    score = 0
    upper_text = ocr_text.upper().replace("Ё", "Е")
    if any(keyword in upper_text for keyword in _PASSPORT_CONTEXT_KEYWORDS):
        score += 5

    if occurrence_count > 1:
        score += 5

    lines = ocr_text.splitlines()
    if 0 <= line_idx < len(lines):
        line = lines[line_idx].strip()
        line_digits = re.sub(r"\D", "", line)
        if line and line_digits and len(line_digits) / len(line) >= 0.5:
            score += 3

    score += 2

    for date_match in re.finditer(r"\b(\d{2})[./](\d{2})[./](\d{4})\b", ocr_text):
        date_digits = "".join(date_match.groups())
        if digits10 == date_digits:
            score -= 10

    for code_match in re.finditer(r"\b(\d{3})-(\d{3})\b", ocr_text):
        code_digits = "".join(code_match.groups())
        if digits10 == code_digits:
            score -= 10

    for inn_match in re.finditer(r"(?<!\d)(\d{12})(?!\d)", ocr_text):
        if digits10 in inn_match.group(1):
            score -= 10

    for snils_match in re.finditer(
        r"\b(\d{3})[-\s]?(\d{3})[-\s]?(\d{3})[-\s]?(\d{2})\b",
        ocr_text,
    ):
        snils_digits = "".join(snils_match.groups())
        if len(snils_digits) == 11 and digits10 in snils_digits:
            score -= 10

    if _is_excluded_passport_digits(digits10, ocr_text):
        score -= 10

    return score


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
