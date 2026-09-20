from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.handlers.n8n_chat import handle_intake_add_client_callback
from app.services.compact_action_token import create_compact_action_token, telegram_callback_data


@pytest.mark.asyncio
async def test_intake_callback_receives_injected_services_and_reaches_customer_service():
    session_id = uuid4()
    callback = SimpleNamespace(
        data=telegram_callback_data(create_compact_action_token(
            action="intake.add_client", session_id=session_id, channel="telegram",
            external_user_id="42", conversation_id="99",
        )),
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat=SimpleNamespace(id=99), answer=AsyncMock(), edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    intake = AsyncMock()
    customer = AsyncMock()
    customer.add_client.return_value = SimpleNamespace(status="missing_passport", message="Заполните паспорт")

    await handle_intake_add_client_callback(
        callback,
        settings=SimpleNamespace(mini_app_token_secret="legacy-secret"),
        intake_service=intake,
        intake_customer_service=customer,
        database=None,
    )

    intake.validate_session_owner.assert_awaited_once()
    customer.add_client.assert_awaited_once()
