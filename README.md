# IssuerGraph — IIFL Finance vertical slice

One issuer, six real public documents, end to end. Every extracted fact carries a
character-exact anchor into the page it came from; disagreements between sources
become conflict rows rather than a silently chosen number.

## The acceptance test

> open IIFL Finance → see a debt/rating fact → click it → see the exact source
> evidence → see when another document disagrees with it.

`tests/test_evidence.py` asserts this end to end (14 tests).

## Run it

```bash
brew services start postgresql@14
createdb issuergraph
psql -d issuergraph -f sql/schema.sql

uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python fastapi 'uvicorn[standard]' 'psycopg[binary]' \
    pydantic pymupdf python-dateutil pytest

.venv/bin/python -m issuergraph.pipeline --reset     # fetch → ingest → extract → reconcile → diff
.venv/bin/python -m pytest tests -q
.venv/bin/uvicorn issuergraph.api:app --port 8077    # open http://localhost:8077
```

`ISSUERGRAPH_DSN` overrides the default `postgresql:///issuergraph`.

## The corpus

| Document | Source | Published | Pages |
|---|---|---|---|
| Integrated Annual Report 2024-25 | Company | 08 May 2025 | 567 |
| Long-term ratings placed on Watch (Negative) | ICRA | 12 Mar 2024 | 10 |
| Ratings reaffirmed, outlook revised to Negative | ICRA | 24 Sep 2025 | 12 |
| Ratings reaffirmed | ICRA | 11 Feb 2026 | 12 |
| Press release (Revised) | CARE | 12 Mar 2024 | 6 |
| Rating rationale | Brickwork | 15 Sep 2025 | 9 |

Fetched over HTTP from the publishers' own URLs; each stored with its SHA-256,
URL and retrieval timestamp. Re-running the pipeline is idempotent on the hash.

**324 claims** extracted, every one anchored.

## What it found

Four conflicts, all real:

| Fact | Disagreement |
|---|---|
| `rating_outlook\|long_term\|2025Q3` | Brickwork says **Stable**, ICRA says **Negative** — two weeks apart |
| `rating_grade\|long_term\|2025Q3` | Brickwork **AA+**, ICRA **AA** — one notch |
| `rating_watch\|long_term\|2024Q1` | On the *same day*, ICRA said Watch with **Negative** Implications, CARE said Watch with **Developing** Implications |
| `total_borrowings\|standalone\|2024-03-31` | Brickwork ₹20,011 Cr vs annual report ₹19,985.90 Cr — ₹25.10 Cr, 0.126% |

Three facts independently corroborated (consolidated FY24/FY25 and standalone
FY25 total borrowings, agreeing within tolerance).

Nine tracked changes between successive ICRA reports, including the Sept 2025 →
Feb 2026 liquidity narrative moving from ₹3,791 Cr unencumbered cash (Jul 2025)
to ₹5,971 Cr (Dec 2025), and the Mar 2024 → Sep 2025 transition from a rating
watch to a Negative outlook.

## Design

Read `docs/SCHEMA.md` first — it explains the nine tables and the invariants.

Three things are worth calling out:

**Page text is built from word boxes, not `get_text()`.** Words are joined by
spaces within a line and newlines between lines, and each word's char range and
rectangle go into `document_page.word_map`. Any character range therefore maps
to exact rectangles, so highlighting is a lookup — never a re-search of the page
that might land on the wrong occurrence.

**The loader refuses to store a fact it cannot prove.** `load_claims` asserts
`anchor.evidence_text == page.text[char_start:char_end]` for every anchor and
raises `EvidenceMismatch` — aborting the whole ingest — on any drift.

**Derived numbers show their arithmetic.** Ind AS balance sheets have no "total
borrowings" line, so the total is computed from three rows; the claim carries
three anchors and the UI highlights all three cells on page 438.

## Layout

```
sql/schema.sql              nine tables
docs/SCHEMA.md              why each one exists
issuergraph/
  models.py                 Pydantic contract; an anchorless claim cannot be built
  ingest.py                 fetch, SHA-256, page text + geometry
  loader.py                 persists claims, verifies every anchor
  extractors/
    common.py               offset-preserving parse helpers
    icra.py                 rating table, ISIN annexure, strengths/challenges,
                            liquidity, sensitivities
    care.py                 facilities table, liquidity, factors
    brickwork.py            particulars table, Total Debt rows, bullets
    annual_report.py        consolidated + standalone balance sheets
  reconcile.py              conflict detection (0.10% numeric tolerance)
  diff.py                   rationale-to-rationale diff
  corpus.py                 the six sources, declared
  pipeline.py               the ordered run
  api.py                    read-only API + highlighted page renderer
static/index.html           the UI
scripts/tryextract.py       dry-run an extractor against a PDF, no database
```

## Deliberately absent

No score, no recommendation, no chat, no vector store, no agent framework, no
queue, no auth, no tenancy. Orchestration is a function that runs in order.

## Known limits of this slice

- Four extractors, tuned to four publishers' current layouts. A format change
  breaks parsing loudly (offset assertions fail) rather than silently.
- Only ICRA has ≥2 reports here, so only ICRA produces diffs.
- Rationale bullets are diffed on their headline text, so a reworded strength
  reads as one removal plus one addition.
- The numeric tolerance (0.10%) is a single declared constant in
  `reconcile.py`, not a per-fact policy.
- CARE and Brickwork publish no instrument-level maturity table, so the maturity
  ladder comes from ICRA's Annexure I only.
