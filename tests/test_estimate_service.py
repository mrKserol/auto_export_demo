from __future__ import annotations

from decimal import Decimal

from app.services.estimate_service import calculate_estimate


def _make_spec(price: str = "100000", year: str = "2023") -> dict:
    return {"price": price, "year": year, "price_currency": "CNY"}


def test_calculate_estimate_no_calcus():
    spec = _make_spec("100000", "2023")
    engine_power = Decimal("150")
    exchange_rate = Decimal("10")
    inspect_transport_price = Decimal("1000")
    bank_commission = Decimal("2500")
    transit_declaration_price = Decimal("100")
    insurance_shipment = Decimal("200")
    custom_clearing = Decimal("300")
    contractor_comission = Decimal("400")

    out = calculate_estimate(
        specification=spec,
        engine_power=engine_power,
        exchange_rate=exchange_rate,
        inspect_transport_price=inspect_transport_price,
        bank_commission=bank_commission,
        transit_declaration_price=transit_declaration_price,
        insurance_shipment=insurance_shipment,
        custom_clearing=custom_clearing,
        contractor_comission=contractor_comission,
        calcus_result=None,
        calcus_error=None,
        calcus_warnings=None,
    )

    # price_abroad = 100000 + 1000 = 101000
    # price_rub = 101000 * 10 = 1_010_000
    # extra_costs = bank_commission + transit + insurance + clearing + contractor = 2500+100+200+300+400 = 3500
    # total = 1010000? Wait price_rub 1,010,000 + 3,500 = 1,013,500
    assert out["price_rub"] == Decimal("1010000.00")
    assert out["bank_commission"] == Decimal("2500.00")
    assert out["total_rub"] == Decimal("1013500.00")


def test_calculate_estimate_with_calcus():
    spec = _make_spec("100000", "2023")
    engine_power = Decimal("150")
    exchange_rate = Decimal("10")
    inspect_transport_price = Decimal("1000")
    bank_commission = Decimal("2500")
    transit_declaration_price = Decimal("100")
    insurance_shipment = Decimal("200")
    custom_clearing = Decimal("300")
    contractor_comission = Decimal("400")

    # simulate calcus response with total2 = 900000
    calcus_response = {
        "sbor": "0",
        "tax": "0",
        "util": "0",
        "nds": "0",
        "excise": "0",
        "total": "0",
        "total2": "900000",
    }

    out = calculate_estimate(
        specification=spec,
        engine_power=engine_power,
        exchange_rate=exchange_rate,
        inspect_transport_price=inspect_transport_price,
        bank_commission=bank_commission,
        transit_declaration_price=transit_declaration_price,
        insurance_shipment=insurance_shipment,
        custom_clearing=custom_clearing,
        contractor_comission=contractor_comission,
        calcus_result=calcus_response,
        calcus_error=None,
        calcus_warnings=None,
    )

    # extra_costs = 3500 as above; total = customs_total2 + extra_costs = 900000 + 3500 = 903500
    assert out["customs_total2"] == Decimal("900000.00")
    assert out["total_rub"] == Decimal("903500.00")

