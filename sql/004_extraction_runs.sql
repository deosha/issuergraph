-- Migration 004: version-aware reprocessing.
--
-- Before: the pipeline skipped any document that already had claims. Fixing an
-- extractor therefore had no effect on documents already ingested — the fix
-- applied only to issuers loaded afterwards, and the two populations silently
-- diverged. `claim.extractor_version` was recorded and never compared, so the
-- database could not answer "which of these facts came from the broken parser".
--
-- After: a document is re-extracted when its extractor's version no longer
-- matches the version that produced its claims. Claims are derived artifacts
-- and are replaced; what is preserved is the history that is not derivable —
--
--   * extraction_run keeps one row per extraction of a document, so the
--     sequence of parser versions applied to it survives the claims themselves;
--   * conflict rows are upserted on (issuer_id, fact_key, kind), so
--     first_detected_at survives a reprocess even though the claim ids
--     underneath it change.
--
-- Idempotent. Safe to run against a database created by the pre-004 schema.

BEGIN;

CREATE TABLE IF NOT EXISTS extraction_run (
    id                BIGSERIAL PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    extractor         TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    -- why this run happened: 'initial', 'version_changed', 'forced'
    reason            TEXT NOT NULL,
    previous_version  TEXT,                 -- null on the first extraction
    claims_written    INT NOT NULL,
    claims_replaced   INT NOT NULL DEFAULT 0,
    coverage_status   TEXT NOT NULL,
    ran_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS extraction_run_document_idx
    ON extraction_run (document_id, ran_at DESC);

COMMIT;
