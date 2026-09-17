-- Migration 007: withdrawn ratings end, and ended disagreements are not open.
--
-- Two defects in how rating history was read, both of the same shape: a period
-- that the documents say has ended was being treated as still running.
--
--   * A rating "reaffirmed and withdrawn" was given an open-ended state. The
--     state builder closed a state only when the same agency next acted on the
--     same instrument class, and never looked at the action itself — so ICRA's
--     withdrawn commercial-paper IPO programme read as current, and would have
--     been compared against another agency's live rating. A withdrawal is a
--     terminal state on that tranche: it is in force on the action date and
--     not after. It is stored as an empty half-open interval, flagged, so it
--     never overlaps anything and the timeline shows why it ended.
--
--   * A rating conflict whose window had closed — CARE's Developing watch
--     against ICRA's Negative watch, over from 24 September 2025 when ICRA
--     moved to an outlook — was recorded as open on every run, because "open"
--     meant only "detected again this run". resolved_at keeps that meaning
--     (detection stopped; the row is history). ended_on is different: the
--     sources themselves say the disagreement ended on this date. A conflict
--     is in force only when neither is set.
--
--   * A change feed entry saying a fact was "removed" in the later report was
--     asserted even when that report's extraction was incomplete — when the
--     honest statement is "not found", not "no longer there". Each diff now
--     carries whether the document that lacks the item met its own coverage
--     declaration. This only means something once the rating extractors
--     declare real requirements, which they now do: the rating table, the
--     liquidity assessment and the rationale sections are required
--     individually, so a rationale that parsed 87 bullet points and zero
--     ratings is incomplete rather than complete.
--
-- Idempotent.

BEGIN;

ALTER TABLE rating_state
    ADD COLUMN IF NOT EXISTS withdrawn BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE rating_state DROP CONSTRAINT IF EXISTS rating_state_effective_to_check;
ALTER TABLE rating_state DROP CONSTRAINT IF EXISTS rating_state_check;
ALTER TABLE rating_state ADD CONSTRAINT rating_state_check
    CHECK (effective_to IS NULL
           OR effective_to > effective_from
           OR (withdrawn AND effective_to = effective_from));

ALTER TABLE conflict
    ADD COLUMN IF NOT EXISTS ended_on DATE;   -- the sources say it ended here

-- 'unconfirmed': the report that lacks the item did not meet its coverage
-- declaration, so its absence may be a parse failure rather than a change.
ALTER TABLE rationale_diff
    ADD COLUMN IF NOT EXISTS certainty TEXT NOT NULL DEFAULT 'confirmed';
DO $$
BEGIN
    ALTER TABLE rationale_diff ADD CONSTRAINT rationale_diff_certainty_check
        CHECK (certainty IN ('confirmed', 'unconfirmed'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
