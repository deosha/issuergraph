-- Migration 002: conflict history + normalized claim values.
--
-- Before: reconcile() deleted every conflict row each run, so detected_at was
-- always "the last pipeline run" and the ON CONFLICT DO UPDATE branch was
-- unreachable. We could not answer "when did this first appear" or "has it
-- persisted across quarters" — which is the whole point of surveillance.
--
-- After: conflicts are upserted. first_detected_at is written once and never
-- touched; last_seen_at moves forward on every run that still sees the
-- disagreement; a conflict that stops recurring is stamped resolved_at rather
-- than deleted, because a disagreement disappearing is itself a signal.
--
-- Idempotent. Safe to run against a database created by the pre-002 schema.

BEGIN;

ALTER TABLE claim
    ADD COLUMN IF NOT EXISTS normalized_value TEXT;

CREATE INDEX IF NOT EXISTS claim_issuer_normalized_value_idx
    ON claim (issuer_id, normalized_value);

-- Backfill with the same rule the loader applies: collapse whitespace, casefold.
UPDATE claim
   SET normalized_value = nullif(lower(regexp_replace(btrim(value_text), '\s+', ' ', 'g')), '')
 WHERE value_text IS NOT NULL AND normalized_value IS NULL;

ALTER TABLE conflict
    ADD COLUMN IF NOT EXISTS first_detected_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_seen_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_at       TIMESTAMPTZ;

-- Carry the old single timestamp forward as both bounds; it is the best
-- evidence we have of when these rows were observed.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'conflict' AND column_name = 'detected_at') THEN
        EXECUTE 'UPDATE conflict SET first_detected_at = coalesce(first_detected_at, detected_at),
                                     last_seen_at      = coalesce(last_seen_at, detected_at)';
        EXECUTE 'ALTER TABLE conflict DROP COLUMN detected_at';
    END IF;
END $$;

UPDATE conflict SET first_detected_at = coalesce(first_detected_at, now()),
                    last_seen_at      = coalesce(last_seen_at, now());

ALTER TABLE conflict
    ALTER COLUMN first_detected_at SET NOT NULL,
    ALTER COLUMN first_detected_at SET DEFAULT now(),
    ALTER COLUMN last_seen_at      SET NOT NULL,
    ALTER COLUMN last_seen_at      SET DEFAULT now();

CREATE INDEX IF NOT EXISTS conflict_issuer_resolved_first_idx
    ON conflict (issuer_id, resolved_at, first_detected_at);

COMMIT;
