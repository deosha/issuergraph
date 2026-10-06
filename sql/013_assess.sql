-- Migration 013: IssuerGraph Assess — a private, case-scoped borrower workflow.
--
-- Everything lives in its own schema, `assess`, and shares no table with the
-- issuer catalogue: an applicant is never an issuer, and nothing here is read
-- by /app, /demo, the snapshot export or the reconciler.
--
--   * case_file / document / page: what was received and its page text, built
--     exactly as the issuer pipeline builds it (word boxes → word_map), so an
--     anchor's character spans map to rectangles by lookup.
--   * item / fact: extracted facts grouped into the thing they describe (a
--     salary slip, a bureau tradeline, a statement). A fact carries its anchor:
--     `spans` into one page's text (verified when stored), a JSON path for ITR
--     JSON, or — for a model-proposed value whose quote could not be found —
--     verified = false and no spans, which the UI shows as unverified.
--   * txn: bank-statement rows, each anchored to the words it was read from.
--   * correction / assumption / decision / review_ack: everything an analyst
--     changes. Originals are never updated; the effective value is derived.
--   * extraction_run: per-attempt accounting (pages, OCR pages, model calls,
--     tokens, retries, errors). No cost figures are stored or invented.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS assess;

CREATE TABLE IF NOT EXISTS assess.case_file (
    id                BIGSERIAL PRIMARY KEY,
    label             TEXT NOT NULL,
    applicant_name    TEXT,
    borrower_type     TEXT NOT NULL DEFAULT 'salaried'
                      CHECK (borrower_type IN ('salaried','proprietorship','partnership','private_limited')),
    requested_product TEXT,
    requested_amount  NUMERIC,
    is_synthetic      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assess.document (
    id             BIGSERIAL PRIMARY KEY,
    case_id        BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('salary_slip','bank_statement','credit_report',
                                                  'itr_json','od_sanction','other')),
    filename       TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    byte_size      BIGINT NOT NULL,
    stored_path    TEXT NOT NULL,
    media_type     TEXT NOT NULL,
    page_count     INT NOT NULL DEFAULT 0,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received','processing','complete','incomplete',
                                     'failed','unsupported')),
    status_reasons JSONB NOT NULL DEFAULT '[]',
    processed_at   TIMESTAMPTZ,
    source_label   TEXT,             -- bank / bureau / employer, as read
    doc_date       DATE,
    period_start   DATE,
    period_end     DATE,
    summary        JSONB NOT NULL DEFAULT '{}',
    UNIQUE (case_id, sha256)
);

CREATE TABLE IF NOT EXISTS assess.page (
    document_id BIGINT NOT NULL REFERENCES assess.document(id) ON DELETE CASCADE,
    page_no     INT NOT NULL,
    text        TEXT NOT NULL,
    width       REAL NOT NULL,
    height      REAL NOT NULL,
    word_map    JSONB NOT NULL,
    text_source TEXT NOT NULL CHECK (text_source IN ('pdf_text','textract')),
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS assess.extraction_run (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES assess.document(id) ON DELETE CASCADE,
    extractor     TEXT NOT NULL,
    version       TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',
    pages         INT NOT NULL DEFAULT 0,
    ocr_pages     INT NOT NULL DEFAULT 0,
    model_id      TEXT,
    model_calls   INT NOT NULL DEFAULT 0,
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    retries       INT NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS assess.item (
    id          BIGSERIAL PRIMARY KEY,
    case_id     BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    document_id BIGINT NOT NULL REFERENCES assess.document(id) ON DELETE CASCADE,
    item_type   TEXT NOT NULL CHECK (item_type IN ('salary_slip','tradeline','credit_report',
                                                     'statement','itr','od_sanction')),
    seq         INT NOT NULL DEFAULT 0,
    label       TEXT
);

CREATE TABLE IF NOT EXISTS assess.fact (
    id            BIGSERIAL PRIMARY KEY,
    item_id       BIGINT NOT NULL REFERENCES assess.item(id) ON DELETE CASCADE,
    case_id       BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    document_id   BIGINT NOT NULL REFERENCES assess.document(id) ON DELETE CASCADE,
    field         TEXT NOT NULL,
    value_kind    TEXT NOT NULL CHECK (value_kind IN ('amount','date','text','int')),
    value_num     NUMERIC,
    value_text    TEXT,
    value_date    DATE,
    -- Where it came from. 'parser' = deterministic code; 'model' = proposed by
    -- a language model and then checked against the page; 'json' = ITR JSON.
    origin        TEXT NOT NULL CHECK (origin IN ('parser','model','json')),
    verified      BOOLEAN NOT NULL,
    page_no       INT,
    spans         JSONB,            -- [[char_start, char_end], ...] into page text
    rects         JSONB,            -- [[x0,y0,x1,y1], ...] from word_map / OCR boxes
    evidence_text TEXT,             -- verified: the spanned text; else the model's quote
    json_path     TEXT,
    note          TEXT,
    CHECK (NOT verified OR spans IS NOT NULL OR json_path IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS fact_item ON assess.fact(item_id);

CREATE TABLE IF NOT EXISTS assess.txn (
    id            BIGSERIAL PRIMARY KEY,
    case_id       BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    document_id   BIGINT NOT NULL REFERENCES assess.document(id) ON DELETE CASCADE,
    account_key   TEXT NOT NULL,
    seq           INT NOT NULL,
    txn_date      DATE NOT NULL,
    value_date    DATE,
    narration     TEXT NOT NULL,
    ref           TEXT,
    debit         NUMERIC,
    credit        NUMERIC,
    balance       NUMERIC,          -- signed: negative = overdrawn
    page_no       INT NOT NULL,
    spans         JSONB NOT NULL,
    rects         JSONB NOT NULL,
    evidence_text TEXT NOT NULL,
    dup_of        BIGINT REFERENCES assess.txn(id) ON DELETE SET NULL,
    category      TEXT NOT NULL DEFAULT 'other',
    category_basis TEXT
);
CREATE INDEX IF NOT EXISTS txn_case ON assess.txn(case_id, account_key, txn_date);

-- Analyst corrections: never an UPDATE of the original. The newest row for a
-- (target, target_id, field) is the effective value; older rows are history.
CREATE TABLE IF NOT EXISTS assess.correction (
    id         BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    target     TEXT NOT NULL CHECK (target IN ('fact','txn','obligation')),
    target_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    original   JSONB,
    value      JSONB,
    reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Analyst-entered worksheet inputs. History is kept: newest row wins.
CREATE TABLE IF NOT EXISTS assess.assumption (
    id         BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      NUMERIC,
    basis      TEXT NOT NULL CHECK (length(trim(basis)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Analyst decisions on inferred bureau ↔ bank matches. Newest row wins.
CREATE TABLE IF NOT EXISTS assess.decision (
    id         BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    match_key  TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('confirmed','rejected')),
    reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A review item the analyst has looked at and accepted with a note.
CREATE TABLE IF NOT EXISTS assess.review_ack (
    id         BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES assess.case_file(id) ON DELETE CASCADE,
    item_key   TEXT NOT NULL,
    note       TEXT NOT NULL CHECK (length(trim(note)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
