from __future__ import annotations

import unittest

from app.services.customer_batch_data_service import (
    assemble_customer_data_from_batch_files,
    format_customer_data_preview,
)
from app.services.customer_document_recognition_service import _postprocess_passport_main


SAMPLE_OCR = """
Паспорт гражданина Российской Федерации
8006 035956
Фамилия ИВАНОВ
Имя ИВАН
Отчество ИВАНОВИЧ
Дата рождения
12.06.1981
Место рождения
Р.П. АРЬЯ
УРЕНСКОГО Р-НА
ГОРЬКОВСКОЙ ОБЛ.
Дата выдачи
15.03.2005
Код подразделения 520-001
Кем выдан ОУФМС
"""


class PassportBirthFieldsTests(unittest.TestCase):
    def test_explicit_ocr_patronymic_maps_to_surname(self) -> None:
        fields = _postprocess_passport_main(
            {"last_name": "ГУБАЙДУЛИНА", "first_name": "ИРИНА", "surname": "ГУБАЙДУЛИНА"},
            "ФАМИЛИЯ\nГУБАЙДУЛИНА\nИМЯ\nИРИНА\nОТЧЕСТВО\nВЛАДИМИРОВНА",
        )
        self.assertEqual(fields["last_name"], "ГУБАЙДУЛИНА")
        self.assertEqual(fields["first_name"], "ИРИНА")
        self.assertEqual(fields["surname"], "ВЛАДИМИРОВНА")

    def test_passport_surname_does_not_duplicate_last_name_without_explicit_patronymic(self) -> None:
        fields = _postprocess_passport_main(
            {"last_name": "ГУБАЙДУЛИНА", "first_name": "ИРИНА", "surname": "ГУБАЙДУЛИНА"},
            "ФАМИЛИЯ\nГУБАЙДУЛИНА\nИМЯ\nИРИНА",
        )
        self.assertNotEqual(fields.get("surname"), fields.get("last_name"))
    def test_gpt_birth_date_passes_postprocess(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "first_name": "ИВАН",
                "last_name": "ИВАНОВ",
                "surname": "ИВАНОВИЧ",
                "birth_date": "12.06.1981",
                "birth_place": None,
                "date_issue": "15.03.2005",
                "department_code": "520-001",
            },
            SAMPLE_OCR,
        )
        self.assertEqual(fields["birth_date"], "12.06.1981")

    def test_birth_date_12_06_1981_preserved(self) -> None:
        fields = _postprocess_passport_main(
            {"birth_date": "12.06.1981", "passport": "8006035956"},
            "Дата выдачи 15.03.2005",
        )
        self.assertEqual(fields["birth_date"], "12.06.1981")

    def test_gpt_birth_place_passes_postprocess(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "birth_place": "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
            },
            SAMPLE_OCR,
        )
        self.assertEqual(
            fields["birth_place"],
            "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
        )

    def test_multiline_birth_place_readable(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "birth_place": "Р.П. АРЬЯ\nУРЕНСКОГО Р-НА\nГОРЬКОВСКОЙ ОБЛ.",
            },
            "Паспорт",
        )
        self.assertEqual(
            fields["birth_place"],
            "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
        )

    def test_birth_place_abbreviations_kept(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "birth_place": "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
            },
            "Паспорт",
        )
        place = fields["birth_place"]
        self.assertIn("Р.П.", place)
        self.assertIn("Р-НА", place)
        self.assertIn("ОБЛ.", place)

    def test_ocr_fallback_fills_birth_date_when_gpt_null(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "birth_date": None,
                "birth_place": None,
                "date_issue": None,
            },
            SAMPLE_OCR,
        )
        self.assertEqual(fields["birth_date"], "12.06.1981")

    def test_ocr_fallback_does_not_replace_gpt_birth_date(self) -> None:
        fields = _postprocess_passport_main(
            {
                "passport": "8006035956",
                "birth_date": "01.01.1990",
            },
            SAMPLE_OCR,
        )
        self.assertEqual(fields["birth_date"], "01.01.1990")

    def test_issue_date_not_used_as_birth_date(self) -> None:
        ocr = """
        Паспорт
        8006 035956
        Дата выдачи
        15.03.2005
        Код подразделения 520-001
        """
        fields = _postprocess_passport_main(
            {"passport": "8006035956", "birth_date": None},
            ocr,
        )
        self.assertNotEqual(fields.get("birth_date"), "15.03.2005")
        self.assertIsNone(fields.get("birth_date"))

    def test_customer_batch_data_service_gets_both_fields(self) -> None:
        files = [
            {
                "id": 1,
                "detected_document_type": "passport_main",
                "extracted_json": {
                    "fields": {
                        "last_name": "ИВАНОВ",
                        "first_name": "ИВАН",
                        "passport": "8006035956",
                        "birth_date": "12.06.1981",
                        "birth_place": "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
                    }
                },
            }
        ]
        assembled = assemble_customer_data_from_batch_files(files)
        self.assertEqual(assembled.fields["birth_date"], "12.06.1981")
        self.assertEqual(
            assembled.fields["birth_place"],
            "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
        )

    def test_telegram_preview_shows_both_fields(self) -> None:
        files = [
            {
                "id": 1,
                "detected_document_type": "passport_main",
                "extracted_json": {
                    "fields": {
                        "last_name": "ИВАНОВ",
                        "first_name": "ИВАН",
                        "passport": "8006035956",
                        "birth_date": "12.06.1981",
                        "birth_place": "Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
                    }
                },
            }
        ]
        assembled = assemble_customer_data_from_batch_files(files)
        preview = format_customer_data_preview(assembled)
        self.assertIn("Дата рождения: 12.06.1981", preview)
        self.assertIn(
            "Место рождения: Р.П. АРЬЯ УРЕНСКОГО Р-НА ГОРЬКОВСКОЙ ОБЛ.",
            preview,
        )


if __name__ == "__main__":
    unittest.main()
