-- Migration 006: pilot requests.
--
-- Lead capture for the public site. Deliberately narrow: what a pilot
-- conversation needs to be scoped, and nothing more. No tracking identifiers,
-- no enrichment columns, no marketing consent flags — the form says the request
-- is used to reply about a pilot, so the schema cannot quietly hold more.
--
-- The submission is stored before the visitor is shown success, so "we got it"
-- is never a claim about an insert that did not happen.
--
-- Idempotent. Safe to run against a database created by the pre-006 schema.

BEGIN;

CREATE TABLE IF NOT EXISTS pilot_request (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    -- Lower-cased email, for duplicate detection only. A resubmission updates
    -- the existing row rather than creating a second lead: people correct a
    -- typo and send again, and two rows would mean two replies.
    email_key     TEXT NOT NULL UNIQUE,
    organisation  TEXT NOT NULL,
    role          TEXT NOT NULL,
    issuers       TEXT NOT NULL,
    workflow      TEXT NOT NULL,
    notes         TEXT,                                  -- optional
    source        TEXT,                                  -- which page sent it
    submissions   INT NOT NULL DEFAULT 1,                -- how many times they wrote
    crm_forwarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pilot_request_created_idx ON pilot_request (created_at DESC);

-- Rate limiting state. Keyed by a salted hash of the client address, never the
-- address itself: throttling abuse does not require keeping a log of who
-- visited, and a hash cannot be read back into an IP.
CREATE TABLE IF NOT EXISTS pilot_submission_log (
    id          BIGSERIAL PRIMARY KEY,
    client_hash TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pilot_submission_log_idx
    ON pilot_submission_log (client_hash, created_at DESC);

COMMIT;
