-- Migration 005: effective-dated rating states.
--
-- Before: ratings were compared inside calendar-quarter buckets, on a fact key
-- of the form rating_grade|long_term|2025Q3, whose value came from whichever
-- row of the agency's table the extractor happened to read first. Two defects
-- compounded:
--
--   * identity — CARE's first row was "Long Term Bank Facilities" and ICRA's
--     was "Non-convertible debenture programme", so a "conflict" could compare
--     an agency's view of one instrument against another agency's view of a
--     different one;
--   * timing — a quarter both merges actions that merely landed in the same
--     three months and splits genuinely concurrent views that straddle a
--     boundary. Two agencies rating on 30 June and 2 July never meet.
--
-- After: a rating action is in force from its action date until the same agency
-- next acts on the same instrument class. Comparison is between agencies whose
-- intervals actually overlap, on the same instrument class and rating scale.
-- This replaces the TODO(effective-dating) note in reconcile.py.
--
-- rating_state is derived from rating_action and rebuilt each run, so it carries
-- no history of its own — conflict rows hold that.
--
-- Idempotent. Safe to run against a database created by the pre-005 schema.

BEGIN;

ALTER TABLE rating_action
    ADD COLUMN IF NOT EXISTS instrument_class TEXT NOT NULL DEFAULT 'other',
    ADD COLUMN IF NOT EXISTS term             TEXT NOT NULL DEFAULT 'long_term';

DO $$
BEGIN
    ALTER TABLE rating_action ADD CONSTRAINT rating_action_term_check
        CHECK (term IN ('long_term', 'short_term'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS rating_state (
    id               BIGSERIAL PRIMARY KEY,
    issuer_id        BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    claim_id         BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    agency           TEXT NOT NULL,
    instrument_class TEXT NOT NULL,
    term             TEXT NOT NULL,
    instrument       TEXT NOT NULL,          -- the agency's own wording, for display
    grade            TEXT NOT NULL,
    outlook          TEXT,
    watch            TEXT,
    effective_from   DATE NOT NULL,
    effective_to     DATE,                   -- null = still in force
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS rating_state_overlap_idx
    ON rating_state (issuer_id, instrument_class, term, effective_from);

-- What each member of a conflict actually said about the disputed aspect. A
-- rating claim's value_text is the whole rating line ("[ICRA]AA (Negative);
-- reaffirmed"), which is the right evidence but the wrong answer to "what do
-- they disagree about" — the outlook conflict is Stable against Negative. The
-- claim keeps the verbatim line; this column carries the compared value.
ALTER TABLE conflict_member
    ADD COLUMN IF NOT EXISTS stated_value TEXT;

-- Quarter-keyed rating conflicts are deleted rather than left to be stamped
-- resolved. resolved_at means "this disagreement stopped recurring", and none
-- of these did: they are rows in a retired keying scheme, and marking them
-- resolved would assert something false about the issuer. What the retired
-- comparison found is recorded in git, not in a status column.
DELETE FROM conflict
 WHERE fact_key ~ '^rating_(grade|outlook|watch)\|long_term\|\d{4}Q[1-4]$';

COMMIT;
