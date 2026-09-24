-- Migration 008: a withdrawal may state no grade.
--
-- 007 handled "reaffirmed and withdrawn": the agency states a grade as its last
-- word on the tranche and ends it the same day. CARE's 19 September 2024
-- release withdraws IIFL's Non-Convertible Debentures with no grade at all —
-- the rating cell reads "-", the action cell "Withdrawn". The table could not
-- store that, so the row was dropped by the extractor, the earlier AA state on
-- debentures was never closed, and CARE read as rating the NCDs AA for years
-- after it stopped rating them.
--
-- Filling the grade in from somewhere else (the rating-history annexure, the
-- previous release) would store a value this action does not state. So the
-- grade is nullable, and only for a withdrawal: every other rating still has to
-- say what it is.
--
-- Idempotent.

BEGIN;

ALTER TABLE rating_action ALTER COLUMN rating DROP NOT NULL;
ALTER TABLE rating_action DROP CONSTRAINT IF EXISTS rating_action_grade_or_withdrawn;
ALTER TABLE rating_action ADD CONSTRAINT rating_action_grade_or_withdrawn
    CHECK (rating IS NOT NULL OR action ILIKE '%withdrawn%');

ALTER TABLE rating_state ALTER COLUMN grade DROP NOT NULL;
ALTER TABLE rating_state DROP CONSTRAINT IF EXISTS rating_state_grade_or_withdrawn;
ALTER TABLE rating_state ADD CONSTRAINT rating_state_grade_or_withdrawn
    CHECK (grade IS NOT NULL OR withdrawn);

COMMIT;
