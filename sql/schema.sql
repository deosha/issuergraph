-- IssuerGraph vertical slice schema. See docs/SCHEMA.md for rationale.

DROP TABLE IF EXISTS rationale_diff CASCADE;
DROP TABLE IF EXISTS conflict_member CASCADE;
DROP TABLE IF EXISTS conflict CASCADE;
DROP TABLE IF EXISTS debt_observation CASCADE;
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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
    value_text        TEXT,
    basis             TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (basis IN ('standalone', 'consolidated', 'unknown')),
    as_of_date        DATE,
    extractor         TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (value_numeric IS NOT NULL OR value_text IS NOT NULL)
);
CREATE INDEX ON claim (issuer_id, claim_type);
CREATE INDEX ON claim (fact_key);
CREATE INDEX ON claim (document_id);

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
    instrument    TEXT NOT NULL,
    rated_amount_cr NUMERIC,
    rating        TEXT NOT NULL,
    outlook       TEXT,                     -- Stable / Negative / Positive
    watch         TEXT,                     -- e.g. 'Watch with Negative Implications'
    action        TEXT,                     -- reaffirmed / withdrawn / placed on watch
    previous_rating TEXT,
    action_date   DATE
);
CREATE INDEX ON rating_action (agency, action_date);

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
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issuer_id, fact_key, kind)
);

CREATE TABLE conflict_member (
    conflict_id BIGINT NOT NULL REFERENCES conflict(id) ON DELETE CASCADE,
    claim_id    BIGINT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
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
