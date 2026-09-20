from uuid import uuid4

import pytest

from app.services.compact_action_token import (
    consume_compact_action_token,
    create_compact_action_token,
    telegram_callback_data,
)


def test_telegram_callback_data_is_compact_and_owner_bound():
    token = create_compact_action_token(
        action="intake.add_client", session_id=uuid4(), channel="telegram",
        external_user_id="10", conversation_id="20",
    )
    callback = telegram_callback_data(token)
    assert len(callback.encode()) <= 64
    with pytest.raises(ValueError):
        consume_compact_action_token(token[2:], action=None, channel="telegram",
                                      external_user_id="11", conversation_id="20")


def test_compact_action_token_is_one_time_and_expires():
    token = create_compact_action_token(
        action="intake.add_client", session_id=uuid4(), channel="telegram",
        external_user_id="10", conversation_id="20", ttl_seconds=1,
    )
    record = consume_compact_action_token(token, action="intake.add_client",
                                           channel="telegram", external_user_id="10",
                                           conversation_id="20")
    assert record.action == "intake.add_client"
    with pytest.raises(ValueError):
        consume_compact_action_token(token, action="intake.add_client", channel="telegram",
                                      external_user_id="10", conversation_id="20")
