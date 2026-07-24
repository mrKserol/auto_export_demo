from __future__ import annotations

from app.services.customer_card_service import build_customer_card_keyboard


def flatten_callback_datas(kb):
    out = []
    for row in kb.inline_keyboard:
        for btn in row:
            out.append(btn.callback_data)
    return out


def test_edit_button_present_for_non_admin():
    customer = {"id": 5}
    kb = build_customer_card_keyboard(customer, is_admin=False, has_estimate=False)
    # first row must be edit button
    assert kb.inline_keyboard[0][0].text == "✏️ Изменить данные клиента"
    assert kb.inline_keyboard[0][0].callback_data == f"customer_edit:5"
    callbacks = flatten_callback_datas(kb)
    # delete client should not be present for non-admin
    assert not any(c.startswith("customer_delete:") for c in callbacks)


def test_delete_present_for_admin_and_edit_first():
    customer = {"id": 7}
    kb = build_customer_card_keyboard(customer, is_admin=True, has_estimate=False)
    # edit still first
    assert kb.inline_keyboard[0][0].callback_data == f"customer_edit:7"
    callbacks = flatten_callback_datas(kb)
    assert any(c.startswith("customer_delete:") for c in callbacks)

