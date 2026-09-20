-- Intake -> customer transition. Safe for databases created before this stage.
ALTER TABLE document_intake_sessions
    ADD COLUMN IF NOT EXISTS customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL;

ALTER TABLE document_intake_documents
    ADD COLUMN IF NOT EXISTS customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL;

ALTER TABLE document_intake_review_audit
    ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'review_correction';

ALTER TABLE document_intake_review_audit
    ADD COLUMN IF NOT EXISTS actor_external_user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_customers_passport_normalized
    ON customers ((regexp_replace(upper(passport), '[[:space:]-]', '', 'g')));

CREATE TABLE IF NOT EXISTS channel_action_tokens (
    token_hash TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    session_id UUID NOT NULL,
    channel TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    customer_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_channel_action_tokens_expiry
    ON channel_action_tokens(expires_at);
