-- IssuerGraph schema. See docs/SCHEMA.md for rationale.
-- Includes migrations 002-006; existing databases apply those files instead.

DROP TABLE IF EXISTS pilot_submission_log CASCADE;
DROP TABLE IF EXISTS pilot_request CASCADE;
DROP TABLE IF EXISTS rationale_diff CASCADE;
DROP TABLE IF EXISTS extraction_run CASCADE;
DROP TABLE IF EXISTS conflict_member CASCADE;
DROP TABLE IF EXISTS conflict CASCADE;
DROP TABLE IF EXISTS debt_observation CASCADE;
DROP TABLE IF EXISTS rating_state CASCADE;
DROP TABLE IF EXISTS rating_action CASCADE;
DROP TABLE IF EXISTS evidence_anchor CASCADE;
DROP TABLE IF EXISTS claim CASCADE;
DROP TABLE IF EXISTS document_page CASCADE;
DROP TABLE IF EXISTS document CASCADE;
DROP TABLE IF EXISTS issuer CASCADE;

CREATE TABLE issuer (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    aliases     TEXT[] NOT NULL DEFAULT '{}',
    cin         TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document (
    id              BIGSERIAL PRIMARY KEY,
    issuer_id       BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    doc_type        TEXT NOT NULL
                    CHECK (doc_type IN ('annual_report', 'rating_rationale')),
    source_name     TEXT NOT NULL,          -- 'ICRA', 'CARE', 'Brickwork', 'Company'
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    sha256          TEXT NOT NULL UNIQUE,   -- makes ingestion idempotent
    byte_size       BIGINT NOT NULL,
    local_path      TEXT NOT NULL,
    retrieved_at    TIMESTAMPTZ NOT NULL,   -- when WE fetched it
    published_date  DATE,                   -- what the document says about itself
    page_count      INT NOT NULL,
    -- Extraction coverage: did the extractor find what this document was
    -- declared to contain? Without this, a layout change and an issuer that
    -- stopped disclosing a figure are indistinguishable. See sql/003.
    extraction_status   TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (extraction_status IN ('complete', 'incomplete', 'unknown')),
    extraction_expected INT,
    extraction_found    INT,
    extraction_missing  TEXT[] NOT NULL DEFAULT '{}',
    extracted_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (extraction_status <> 'incomplete' OR cardinality(extraction_missing) > 0)
);
CREATE INDEX ON document (issuer_id, extraction_status);
CREATE INDEX ON document (issuer_id, published_date);

CREATE TABLE document_page (
    document_id  BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no      INT NOT NULL,              -- 1-indexed, as printed/paged in the PDF
    text         TEXT NOT NULL,             -- canonical text, offsets align to word_map
    width        DOUBLE PRECISION NOT NULL,
    height       DOUBLE PRECISION NOT NULL,
    rotation     INT NOT NULL DEFAULT 0,
    word_map     JSONB NOT NULL,            -- [[char_start,char_end,x0,y0,x1,y1], ...]
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE claim (
    id                BIGSERIAL PRIMARY KEY,
    issuer_id         BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    document_id       BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    claim_type        TEXT NOT NULL
                      CHECK (claim_type IN ('total_borrowings', 'debt_instrument',
                                            'rating', 'rationale_point')),
    fact_key          TEXT NOT NULL,        -- reconciliation key
    subject           TEXT NOT NULL,        -- human label, e.g. 'Total borrowings'
    value_numeric     NUMERIC,
    value_unit        TEXT,                 -- 'INR_CRORE'
    value_text        TEXT,                 -- verbatim, for evidence display
    normalized_value  TEXT,                 -- casefolded/whitespace-collapsed, for comparison
    basis             TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (basis IN ('standalone', 'consolidated', 'unknown')),
    as_of_date        DATE,
    extractor         TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (value_numeric IS NOT NULL OR value_text IS NOT NULL)
);
CREATE INDEX ON claim (issuer_id, claim_type);
CREATE INDEX ON claim (issuer_id, normalized_value);
CREATE INDEX ON claim (fact_key);
CREATE INDEX ON claim (document_id);

-- One row per extraction of a document. Claims are derived and get replaced
-- when an extractor is fixed; this is the history that replacement would
-- otherwise erase — which parser version produced a document's facts, and when
-- that changed. See sql/004.
CREATE TABLE extraction_run (
    id                BIGSERIAL PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    extractor         TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    reason            TEXT NOT NULL,        -- initial / version_changed / forced
    previous_version  TEXT,
    claims_written    INT NOT NULL,
    claims_replaced   INT NOT NULL DEFAULT 0,
    coverage_status   TEXT NOT NULL,
    ran_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON extraction_run (document_id, ran_at DESC);

CREATE TABLE evidence_anchor (
    id            BIGSERIAL PRIMARY KEY,
    claim_id      BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    document_id   BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no       INT NOT NULL,
    char_start    INT NOT NULL,
    char_end      INT NOT NULL,
    evidence_text TEXT NOT NULL,
    bbox          JSONB,                    -- union rect [x0,y0,x1,y1]
    bbox_rects    JSONB,                    -- per-line rects for drawing
    ordinal       INT NOT NULL DEFAULT 0,
    CHECK (char_end > char_start),
    FOREIGN KEY (document_id, page_no)
        REFERENCES document_page(document_id, page_no) ON DELETE CASCADE
);
CREATE INDEX ON evidence_anchor (claim_id);

CREATE TABLE rating_action (
    id            BIGSERIAL PRIMARY KEY,
    claim_id      BIGINT NOT NULL UNIQUE REFERENCES claim(id) ON DELETE CASCADE,
    agency        TEXT NOT NULL,
    instrument    TEXT NOT NULL,          -- the agency's own wording
    -- What makes two agencies' ratings comparable: the same instrument class on
    -- the same rating scale. Publisher wording differs for identical
    -- instruments, and one action rates several. See sql/005.
    instrument_class TEXT NOT NULL DEFAULT 'other',
    term          TEXT NOT NULL DEFAULT 'long_term'
                  CHECK (term IN ('long_term', 'short_term')),
    rated_amount_cr NUMERIC,
    rating        TEXT NOT NULL,
    outlook       TEXT,                     -- Stable / Negative / Positive
    watch         TEXT,                     -- e.g. 'Watch with Negative Implications'
    action        TEXT,                     -- reaffirmed / withdrawn / placed on watch
    previous_rating TEXT,
    action_date   DATE
);
CREATE INDEX ON rating_action (agency, action_date);

-- Effective-dated rating timelines, derived from rating_action and rebuilt each
-- run: a rating stands from its action date until the same agency next acts on
-- the same instrument class. Replaces calendar-quarter bucketing. See sql/005.
CREATE TABLE rating_state (
    id               BIGSERIAL PRIMARY KEY,
    issuer_id        BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    claim_id         BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    agency           TEXT NOT NULL,
    instrument_class TEXT NOT NULL,
    term             TEXT NOT NULL,
    instrument       TEXT NOT NULL,
    grade            TEXT NOT NULL,
    outlook          TEXT,
    watch            TEXT,
    effective_from   DATE NOT NULL,
    effective_to     DATE,                 -- null = still in force
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX ON rating_state (issuer_id, instrument_class, term, effective_from);

CREATE TABLE debt_observation (
    id              BIGSERIAL PRIMARY KEY,
    claim_id        BIGINT NOT NULL UNIQUE REFERENCES claim(id) ON DELETE CASCADE,
    instrument_name TEXT NOT NULL,
    instrument_type TEXT NOT NULL,          -- ncd / subordinated_debt / cp / bank_facility / total
    amount_cr       NUMERIC NOT NULL,
    maturity_date   DATE,
    secured         BOOLEAN,
    as_of_date      DATE
);
CREATE INDEX ON debt_observation (instrument_type);

CREATE TABLE conflict (
    id            BIGSERIAL PRIMARY KEY,
    issuer_id     BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    fact_key      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('numeric_disagreement', 'categorical_disagreement')),
    tolerance_pct NUMERIC,
    spread_pct    NUMERIC,
    note          TEXT,
    -- History: a conflict is a durable observation, not a per-run rendering.
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- never updated
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- refreshed each run
    resolved_at       TIMESTAMPTZ,                         -- set when it stops recurring
    UNIQUE (issuer_id, fact_key, kind)
);
CREATE INDEX ON conflict (issuer_id, resolved_at, first_detected_at);

CREATE TABLE conflict_member (
    conflict_id  BIGINT NOT NULL REFERENCES conflict(id) ON DELETE CASCADE,
    claim_id     BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    -- What this member said about the disputed aspect, when that is narrower
    -- than the claim's own verbatim text: 'Negative' out of the full rating
    -- line the claim quotes. Null when the claim's value is the compared value.
    stated_value TEXT,
    PRIMARY KEY (conflict_id, claim_id)
);

CREATE TABLE rationale_diff (
    id            BIGSERIAL PRIMARY KEY,
    issuer_id     BIGINT NOT NULL REFERENCES issuer(id) ON DELETE CASCADE,
    agency        TEXT NOT NULL,
    from_document_id BIGINT REFERENCES document(id) ON DELETE CASCADE,
    to_document_id   BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    from_date     DATE,
    to_date       DATE,
    section       TEXT NOT NULL,            -- 'strengths' / 'weaknesses' / 'rating'
    direction     TEXT NOT NULL
                  CHECK (direction IN ('added', 'removed', 'changed')),
    from_claim_id BIGINT REFERENCES claim(id) ON DELETE CASCADE,
    to_claim_id   BIGINT REFERENCES claim(id) ON DELETE CASCADE,
    from_text     TEXT,
    to_text       TEXT
);
CREATE INDEX ON rationale_diff (issuer_id, agency, to_date);


-- Lead capture for the public site. Narrow by design: what a pilot
-- conversation needs, and nothing more. See sql/006.
CREATE TABLE pilot_request (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    email_key     TEXT NOT NULL UNIQUE,   -- lower-cased, for duplicate handling
    organisation  TEXT NOT NULL,
    role          TEXT NOT NULL,
    issuers       TEXT NOT NULL,
    workflow      TEXT NOT NULL,
    notes         TEXT,
    source        TEXT,
    submissions   INT NOT NULL DEFAULT 1,
    crm_forwarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON pilot_request (created_at DESC);

-- Rate-limiting state, keyed by a salted hash of the client address rather than
-- the address, because throttling does not require a log of who visited.
CREATE TABLE pilot_submission_log (
    id          BIGSERIAL PRIMARY KEY,
    client_hash TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON pilot_submission_log (client_hash, created_at DESC);
