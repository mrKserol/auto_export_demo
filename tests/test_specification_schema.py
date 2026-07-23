from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from app.web.schemas import SpecificationFormIn


def _valid_payload(**overrides):
    payload = {
        "brand": "Buick",
        "model": "Encore",
        "year": 2023,
        "eng_capacity": "1.3",
        "eng_type": "Бензин",
        "drive": "Полный",
        "transmission": "АКПП",
        "color": "белый",
        "complectation": "RS",
        "mileage": 50000,
        "price": "100000",
        "currency": "CNY",
        "context_token": "token.payload",
        "telegram_init_data": "auth_date=1&hash=abc",
    }
    payload.update(overrides)
    return payload


class SpecificationFormSchemaTests(unittest.TestCase):
    def test_valid_form(self) -> None:
        form = SpecificationFormIn.model_validate(_valid_payload())
        fields = form.to_database_fields()
        self.assertEqual(fields["brand"], "Buick")
        self.assertEqual(fields["year"], "2023")
        self.assertEqual(fields["eng_capacity"], "1.3")
        self.assertEqual(fields["price"], "100000")
        self.assertEqual(fields["mileage"], "50000")

    def test_empty_brand(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(brand="  "))

    def test_invalid_year(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(year=1900))
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(
                _valid_payload(year=datetime.now().year + 2)
            )

    def test_negative_mileage(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(mileage=-1))

    def test_zero_price(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(price="0"))

    def test_unknown_eng_type(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(eng_type="Газ"))

    def test_unknown_transmission(self) -> None:
        with self.assertRaises(ValidationError):
            SpecificationFormIn.model_validate(_valid_payload(transmission="CVT"))

    def test_strips_whitespace(self) -> None:
        form = SpecificationFormIn.model_validate(
            _valid_payload(brand="  Buick  ", color=" белый ")
        )
        self.assertEqual(form.brand, "Buick")
        self.assertEqual(form.color, "белый")

    def test_optional_complectation(self) -> None:
        form = SpecificationFormIn.model_validate(_valid_payload(complectation=""))
        self.assertIsNone(form.complectation)
        self.assertIsNone(form.to_database_fields()["complectation"])


if __name__ == "__main__":
    unittest.main()
