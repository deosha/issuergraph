-- Migration 009: rating-history annexures, corpus gaps, and incomplete corpora.
--
-- A rating was treated as in force until the same agency's next *ingested*
-- action, with nothing checking that we held all of that agency's actions.
-- CARE's 12 March 2024 AA therefore read as current long after CARE had
-- revised it twice, and ICRA's 12 March 2024 watch as running to September
-- 2025 when ICRA had dropped it in September 2024. Every agency rationale
-- carries a rating-history annexure listing the agency's own past actions
-- with dates; this migration stores those entries as anchored facts and uses
-- them to find the documents we are missing.
--
--   * claim.provenance separates what a rationale says it is doing now
--     ('primary_rationale') from what its annexure says the agency did before
--     ('history_annexure'). Annual-report claims are 'primary_report'.
--
--   * rating_history_entry is one annexure row: one agency's action on one
--     instrument on one date. A grade may be absent only on a withdrawal.
--
--   * corpus_gap is a history action with no matching primary action (same
--     agency, class and scale; date within 3 days; same grade). Durable like
--     conflict: first_detected_at written once, last_seen_at advanced each
--     run, resolved_at stamped when the primary document is loaded. 'scope'
--     separates gaps inside the period our documents cover from actions that
--     predate the agency's earliest document we hold, which truncate nothing.
--
--   * rating_state records how each period ended (end_basis) and, when it
--     ended — or began — on an action known only from an annexure, which
--     history claim that was. provenance on the state says whether a primary
--     action or a history entry opened it.
--
--   * conflict.incomplete_corpus flags a disagreement computed over a period
--     in which either agency has an open gap. Flagged, never suppressed.
--
-- Idempotent.

BEGIN;

ALTER TABLE claim
    ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'primary_rationale';
UPDATE claim c SET provenance = 'primary_report'
  FROM document d
 WHERE d.id = c.document_id AND d.doc_type = 'annual_report'
   AND c.provenance = 'primary_rationale';
ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_provenance_check;
ALTER TABLE claim ADD CONSTRAINT claim_provenance_check
    CHECK (provenance IN ('primary_rationale', 'primary_report', 'history_annexure'));

ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_claim_type_check;
ALTER TABLE claim ADD CONSTRAINT claim_claim_type_check
    CHECK (claim_type IN ('total_borrowings', 'debt_instrument', 'rating',
                          'rationale_point', 'rating_history'));

CREATE TABLE IF NOT EXISTS rating_history_entry (
    id               BIGSERIAL PRIMARY KEY,
    claim_id         BIGINT NOT NULL UNIQUE REFERENCES claim(id) ON DELETE CASCADE,
    agency           TEXT NOT NULL,
    instrument       TEXT NOT NULL,          -- the annexure's own wording
    instrument_class TEXT NOT NULL,
    term             TEXT NOT NULL CHECK (term IN ('long_term', 'short_term')),
    action_date      DATE NOT NULL,
    grade            TEXT,
    outlook          TEXT,
    watch            TEXT,
    withdrawn        BOOLEAN NOT NULL DEFAULT false,
    CHECK (grade IS NOT NULL OR withdrawn)
);
CREATE INDEX IF NOT EXISTS rating_history_entry_lookup
    ON rating_history_entry (agency, instrument_class, term, action_date);

CREATE TABLE IF NOT EXISTS corpus_gap (
    id                BIGSERIAL PRIMARY KEY,
    issuer_id         BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    agency            TEXT NOT NULL,
    instrument_class  TEXT NOT NULL,
    term              TEXT NOT NULL,
    action_date       DATE NOT NULL,
    grade             TEXT NOT NULL DEFAULT '',   -- '' for a gradeless withdrawal
    outlook           TEXT,
    watch             TEXT,
    withdrawn         BOOLEAN NOT NULL DEFAULT false,
    scope             TEXT NOT NULL CHECK (scope IN ('within_corpus', 'before_corpus')),
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    UNIQUE (issuer_id, agency, instrument_class, term, action_date, grade)
);

-- Every annexure row that lists the missing action. Refreshed each run, like
-- conflict_member; the gap's own identity and history survive.
CREATE TABLE IF NOT EXISTS corpus_gap_evidence (
    gap_id   BIGINT NOT NULL REFERENCES corpus_gap(id) ON DELETE CASCADE,
    claim_id BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    PRIMARY KEY (gap_id, claim_id)
);

-- Derived and rebuilt on every run (sql/005), so emptying it loses nothing and
-- lets the constraint below hold for rows written before end_basis existed.
DELETE FROM rating_state;
ALTER TABLE rating_state
    ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'primary_rationale',
    ADD COLUMN IF NOT EXISTS end_basis TEXT,
    ADD COLUMN IF NOT EXISTS superseded_by_claim_id BIGINT REFERENCES claim(id) ON DELETE CASCADE;
ALTER TABLE rating_state DROP CONSTRAINT IF EXISTS rating_state_provenance_check;
ALTER TABLE rating_state ADD CONSTRAINT rating_state_provenance_check
    CHECK (provenance IN ('primary_rationale', 'history_annexure'));
ALTER TABLE rating_state DROP CONSTRAINT IF EXISTS rating_state_end_basis_check;
ALTER TABLE rating_state ADD CONSTRAINT rating_state_end_basis_check
    CHECK ((effective_to IS NULL) = (end_basis IS NULL)
           AND (end_basis IS NULL OR end_basis IN ('primary', 'history', 'withdrawn'))
           AND ((end_basis = 'history') = (superseded_by_claim_id IS NOT NULL)));

ALTER TABLE conflict
    ADD COLUMN IF NOT EXISTS incomplete_corpus BOOLEAN NOT NULL DEFAULT false;

COMMIT;
