from __future__ import annotations

import base64
import json
import unittest

from app.web.token_service import (
    TokenError,
    create_intake_review_context_token,
    create_specification_context_token,
    verify_intake_review_context_token,
    verify_specification_context_token,
)


SECRET = "test-secret"
NOW = 1_700_000_000


class IntakeReviewContextTokenTests(unittest.TestCase):
    def test_token_is_bound_to_user_and_session(self) -> None:
        token = create_intake_review_context_token(
            secret=SECRET,
            session_id="11111111-1111-1111-1111-111111111111",
            telegram_user_id=456,
            origin_chat_id=456,
            ttl_seconds=900,
            now=NOW,
        )
        context = verify_intake_review_context_token(
            token,
            secret=SECRET,
            expected_telegram_user_id=456,
            now=NOW + 10,
        )
        self.assertEqual(context.session_id, "11111111-1111-1111-1111-111111111111")
        with self.assertRaisesRegex(TokenError, "другому пользователю"):
            verify_intake_review_context_token(
                token,
                secret=SECRET,
                expected_telegram_user_id=999,
                now=NOW + 10,
            )


class SpecificationContextTokenTests(unittest.TestCase):
    def test_create_and_verify_valid_token(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=900,
            now=NOW,
        )
        context = verify_specification_context_token(
            token,
            secret=SECRET,
            expected_telegram_user_id=456,
            now=NOW + 10,
        )
        self.assertEqual(context.customer_id, 123)
        self.assertEqual(context.telegram_user_id, 456)
        self.assertEqual(context.origin_chat_id, -100123)
        self.assertEqual(context.purpose, "create_specification")
        self.assertEqual(context.exp, NOW + 900)

    def test_tampered_payload_is_rejected(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=900,
            now=NOW,
        )
        payload_part, signature_part = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(payload_part + "=="))
        payload["customer_id"] = 999
        tampered_payload = (
            base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .decode("ascii")
            .rstrip("=")
        )
        tampered_token = f"{tampered_payload}.{signature_part}"
        with self.assertRaises(TokenError) as raised:
            verify_specification_context_token(tampered_token, secret=SECRET, now=NOW)
        self.assertEqual(raised.exception.code, "INVALID_CONTEXT_TOKEN")

    def test_tampered_signature_is_rejected(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=900,
            now=NOW,
        )
        payload_part, signature_part = token.split(".", 1)
        tampered_signature = ("A" if not signature_part.startswith("A") else "B") + signature_part[1:]
        with self.assertRaises(TokenError) as raised:
            verify_specification_context_token(
                f"{payload_part}.{tampered_signature}",
                secret=SECRET,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "INVALID_CONTEXT_TOKEN")

    def test_expired_token_is_rejected(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=60,
            now=NOW,
        )
        with self.assertRaises(TokenError) as raised:
            verify_specification_context_token(token, secret=SECRET, now=NOW + 61)
        self.assertEqual(raised.exception.code, "EXPIRED_CONTEXT_TOKEN")

    def test_wrong_purpose_is_rejected(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=900,
            now=NOW,
        )
        payload_part, _signature_part = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(payload_part + "=="))
        payload["purpose"] = "other"
        from app.web import token_service

        forged = token_service._sign_payload(payload, SECRET)
        with self.assertRaises(TokenError) as raised:
            verify_specification_context_token(forged, secret=SECRET, now=NOW)
        self.assertEqual(raised.exception.code, "INVALID_CONTEXT_TOKEN")

    def test_wrong_user_is_rejected(self) -> None:
        token = create_specification_context_token(
            secret=SECRET,
            customer_id=123,
            telegram_user_id=456,
            origin_chat_id=-100123,
            ttl_seconds=900,
            now=NOW,
        )
        with self.assertRaises(TokenError) as raised:
            verify_specification_context_token(
                token,
                secret=SECRET,
                expected_telegram_user_id=999,
                now=NOW,
            )
        self.assertEqual(raised.exception.code, "USER_MISMATCH")


if __name__ == "__main__":
    unittest.main()
