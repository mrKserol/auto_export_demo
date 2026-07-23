from __future__ import annotations

from typing import Any

from app.database import Database


class SpecificationServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class CustomerNotFoundError(SpecificationServiceError):
    def __init__(self, customer_id: int) -> None:
        super().__init__(
            "CUSTOMER_NOT_FOUND",
            "Клиент не найден",
            details={"customer_id": customer_id},
        )


class SpecificationAlreadyExistsError(SpecificationServiceError):
    def __init__(self, customer_id: int, specification_id: int) -> None:
        super().__init__(
            "SPECIFICATION_ALREADY_EXISTS",
            "У клиента уже есть спецификация",
            details={
                "customer_id": customer_id,
                "specification_id": specification_id,
            },
        )


async def create_customer_specification(
    database: Database,
    customer_id: int,
    fields: dict,
    replace_existing: bool = False,
) -> dict:
    customer = await database.get_customer_by_id(int(customer_id))
    if not customer:
        raise CustomerNotFoundError(int(customer_id))

    existing_specification_id = customer.get("specification_id")
    if existing_specification_id and not replace_existing:
        raise SpecificationAlreadyExistsError(
            int(customer_id),
            int(existing_specification_id),
        )

    specification_id = await database.create_specification(fields)
    updated_customer = await database.attach_specification_to_customer(
        int(customer_id),
        int(specification_id),
    )
    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        raise SpecificationServiceError(
            "DATABASE_ERROR",
            "Спецификация создана, но не найдена",
            details={"specification_id": specification_id},
        )

    return {
        "customer": updated_customer,
        "specification": specification,
        "specification_id": int(specification_id),
        "replaced_specification_id": (
            int(existing_specification_id) if existing_specification_id else None
        ),
    }
