-- Migration 003: extraction coverage.
--
-- Before: an extractor that matched nothing returned an empty list and the
-- document loaded clean. A layout change and an issuer that genuinely stopped
-- disclosing a figure produced byte-identical database state, so "this fact
-- disappeared from the report" could not be distinguished from "we stopped
-- parsing it". That is the worst possible failure for a surveillance product.
--
-- After: every document records whether its extractor found what that document
-- was declared to contain, and the specific reasons when it did not. Claims are
-- still stored — they are still evidence — but the gap is now itself a fact.
--
-- 'unknown' is the state of documents ingested before this migration: we did
-- not check, and saying so is more honest than backfilling them as complete.
--
-- Idempotent. Safe to run against a database created by the pre-003 schema.

BEGIN;

ALTER TABLE document
    ADD COLUMN IF NOT EXISTS extraction_status   TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS extraction_expected INT,
    ADD COLUMN IF NOT EXISTS extraction_found    INT,
    ADD COLUMN IF NOT EXISTS extraction_missing  TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS extracted_at        TIMESTAMPTZ;

DO $$
BEGIN
    ALTER TABLE document ADD CONSTRAINT document_extraction_status_check
        CHECK (extraction_status IN ('complete', 'incomplete', 'unknown'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- The invariant the state exists to express: incomplete means we can say why.
DO $$
BEGIN
    ALTER TABLE document ADD CONSTRAINT document_incomplete_has_reasons
        CHECK (extraction_status <> 'incomplete'
               OR cardinality(extraction_missing) > 0);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS document_extraction_status_idx
    ON document (issuer_id, extraction_status);

COMMIT;
