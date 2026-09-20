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
