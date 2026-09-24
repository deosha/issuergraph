-- Migration 012: which gaps matter, and sources that change after we read them.
--
--   * corpus_gap.changes_view: does the missing action change what the agency
--     was saying — its grade, outlook or watch — relative to its preceding view
--     on that class? Crisil reaffirms IIFL at AA/Stable roughly quarterly; a
--     missing reaffirmation leaves every in-force period exactly as stated. A
--     missing downgrade does not. Only the second may block publishing.
--
--   * conflict.material_gap: the difference was computed over a period with an
--     open gap that changes a view. incomplete_corpus stays as the broader
--     flag (any open gap, reaffirmations included) and keeps being shown.
--
--   * document.source_updated_on: a publisher's own "updated on" statement
--     (Crisil: "This RR was updated on May 22, 2026"). Such pages are edited
--     in place at the same URL.
--   * document.source_changed_at / source_changed_sha256: the page at the URL
--     no longer hashes to what we retrieved. Our stored copy stays the evidence
--     of record; this records that the publisher's copy moved on.
--
-- Idempotent.

BEGIN;

ALTER TABLE corpus_gap
    ADD COLUMN IF NOT EXISTS changes_view BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE conflict
    ADD COLUMN IF NOT EXISTS material_gap BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE document
    ADD COLUMN IF NOT EXISTS source_updated_on     DATE,
    ADD COLUMN IF NOT EXISTS source_changed_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_changed_sha256 TEXT;

COMMIT;
