from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ALLOWED_ENG_TYPES = (
    "Бензин",
    "Дизель",
    "Гибрид",
    "Подключаемый гибрид",
    "Электро",
)

ALLOWED_DRIVES = (
    "Передний",
    "Задний",
    "Полный",
)

ALLOWED_TRANSMISSIONS = (
    "МКПП",
    "АКПП",
    "Робот",
    "Вариатор",
    "Редуктор",
)


class SpecificationFormIn(BaseModel):
    brand: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    year: int
    eng_capacity: Decimal
    eng_type: str
    drive: str
    transmission: str
    color: str = Field(min_length=1, max_length=100)
    complectation: str | None = Field(default=None, max_length=200)
    mileage: int
    price: Decimal
    currency: Literal["CNY"] = "CNY"
    context_token: str = Field(min_length=1)
    telegram_init_data: str = Field(min_length=1)

    @field_validator(
        "brand",
        "model",
        "eng_type",
        "drive",
        "transmission",
        "color",
        "context_token",
        "telegram_init_data",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("complectation", mode="before")
    @classmethod
    def normalize_complectation(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return value

    @field_validator("year")
    @classmethod
    def validate_year(cls, value: int) -> int:
        max_year = datetime.now().year + 1
        if value < 1950 or value > max_year:
            raise ValueError(f"Год должен быть в диапазоне 1950–{max_year}")
        return value

    @field_validator("eng_capacity")
    @classmethod
    def validate_eng_capacity(cls, value: Decimal) -> Decimal:
        if value < Decimal("0.1") or value > Decimal("20.0"):
            raise ValueError("Объём двигателя должен быть от 0.1 до 20.0")
        return value

    @field_validator("eng_type")
    @classmethod
    def validate_eng_type(cls, value: str) -> str:
        if value not in ALLOWED_ENG_TYPES:
            raise ValueError("Недопустимый тип двигателя")
        return value

    @field_validator("drive")
    @classmethod
    def validate_drive(cls, value: str) -> str:
        if value not in ALLOWED_DRIVES:
            raise ValueError("Недопустимый тип привода")
        return value

    @field_validator("transmission")
    @classmethod
    def validate_transmission(cls, value: str) -> str:
        if value not in ALLOWED_TRANSMISSIONS:
            raise ValueError("Недопустимый тип КПП")
        return value

    @field_validator("mileage")
    @classmethod
    def validate_mileage(cls, value: int) -> int:
        if value < 0 or value > 5_000_000:
            raise ValueError("Пробег должен быть от 0 до 5 000 000")
        return value

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: Decimal) -> Decimal:
        if value <= 0 or value > Decimal("1000000000"):
            raise ValueError("Стоимость должна быть больше 0 и не больше 1 000 000 000")
        return value

    @model_validator(mode="after")
    def validate_tokens_present(self) -> SpecificationFormIn:
        if not self.context_token:
            raise ValueError("Контекстный токен обязателен")
        if not self.telegram_init_data:
            raise ValueError("Telegram initData обязателен")
        return self

    def to_database_fields(self) -> dict[str, str | None]:
        """Convert validated form to current TEXT-compatible specification fields."""
        eng_capacity = format(self.eng_capacity.normalize(), "f")
        if "." in eng_capacity:
            eng_capacity = eng_capacity.rstrip("0").rstrip(".")
        price = format(self.price.normalize(), "f")
        if "." in price:
            price = price.rstrip("0").rstrip(".")
        return {
            "brand": self.brand,
            "model": self.model,
            "year": str(self.year),
            "eng_capacity": eng_capacity,
            "eng_type": self.eng_type,
            "drive": self.drive,
            "transmission": self.transmission,
            "color": self.color,
            "complectation": self.complectation,
            "mileage": str(self.mileage),
            "price": price,
        }
