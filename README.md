# IssuerGraph — IIFL Finance vertical slice

One issuer, six real public documents, end to end. Every extracted fact carries a
character-exact anchor into the page it came from; disagreements between sources
become conflict rows rather than a silently chosen number.

## The acceptance test

> open IIFL Finance → see a debt/rating fact → click it → see the exact source
> evidence → see when another document disagrees with it.

`tests/test_evidence.py` asserts this end to end; `test_reconcile_fixes.py`,
`test_extraction_coverage.py` and `test_api_scope.py` lock down the loader,
reconciliation, extraction coverage, declared units and issuer scoping
(94 tests total).

## Run it

```bash
brew services start postgresql@14
createdb issuergraph
psql -d issuergraph -f sql/schema.sql
# existing databases only — schema.sql already includes both:
psql -d issuergraph -f sql/002_conflict_history.sql
psql -d issuergraph -f sql/003_extraction_coverage.sql
psql -d issuergraph -f sql/004_extraction_runs.sql

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

Five conflicts, all real:

| Fact | Disagreement |
|---|---|
| `rating_outlook\|long_term\|2025Q3` | Brickwork says **Stable**, ICRA says **Negative** — two weeks apart |
| `rating_grade\|long_term\|2025Q3` | Brickwork **AA+**, ICRA **AA** — one notch |
| `rating_watch\|long_term\|2024Q1` | On the *same day*, ICRA said Watch with **Negative** Implications, CARE said Watch with **Developing** Implications |
| `total_borrowings\|standalone\|2024-03-31` | Brickwork ₹20,011 Cr vs annual report ₹19,985.90 Cr — ₹25.10 Cr, 0.126% |
| `total_borrowings\|consolidated\|2024-03-31` | Brickwork ₹46,699 Cr vs annual report ₹46,674.20 Cr — ₹24.80 Cr, 0.053% |

The last two are one definitional difference in Brickwork's "Total Debt". They
are caught only because agreement is tested two ways: a relative band (0.10%)
*and* an absolute floor (₹5 crore). A percentage-only test flags the standalone
gap and passes the consolidated one, which is ₹0.30 crore smaller.

Two facts independently corroborated (consolidated and standalone FY25 total
borrowings, residual variance ₹0.03 Cr and ₹0.16 Cr — reported rather than
rendered as an exact match).

Nine tracked changes between successive ICRA reports, including the Sept 2025 →
Feb 2026 liquidity narrative moving from ₹3,791 Cr unencumbered cash (Jul 2025)
to ₹5,971 Cr (Dec 2025), and the Mar 2024 → Sep 2025 transition from a rating
watch to a Negative outlook.

## Design

Read `docs/SCHEMA.md` first — it explains the nine tables and the invariants.

Five things are worth calling out:

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

**An anchor proves location, not meaning.** Change a statement's header from
crores to lakhs and every digit stays where it was: every anchor still verifies
and every value is wrong by a factor of 100. So the unit is read from the
statement's own "(₹ in Crores)" declaration, converted if it is not crores, and
anchored alongside each amount — the fourth highlight on a total borrowings
claim is the unit that establishes its magnitude.

**A fact that failed to parse is not a fact the issuer stopped reporting.**
An extractor that matches nothing returns an empty list, and every claim it did
return is still perfectly anchored, so silence is the dangerous failure. Each
document therefore declares what it must yield — the annual report: two bases ×
two comparative columns × (three components + a total) — and is stored
`extraction_status = 'incomplete'` with named reasons when it falls short.
`python -m issuergraph.pipeline --strict` turns that into a non-zero exit.

## Layout

```
sql/schema.sql              nine tables
sql/002..004_*.sql           conflict history; extraction coverage; reprocessing
docs/SCHEMA.md              why each one exists
issuergraph/
  models.py                 Pydantic contract; an anchorless claim cannot be built
  ingest.py                 fetch, SHA-256, page text + geometry
  loader.py                 persists claims, verifies every anchor
  extractors/
    common.py               offset-preserving parse helpers
    units.py                the declared monetary scale, read and anchored
    coverage.py             what each document must yield, and what it missed
    icra.py                 rating table, ISIN annexure, strengths/challenges,
                            liquidity, sensitivities
    care.py                 facilities table, liquidity, factors
    brickwork.py            particulars table, Total Debt rows, bullets
    annual_report.py        consolidated + standalone balance sheets
  reconcile.py              conflict detection (0.10% and ₹5 crore tolerances)
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

- Four extractors, tuned to four publishers' current layouts. A format change is
  caught two ways, and an earlier version of this README overstated the first:
  offset assertions fail loudly only when a pattern *matches* and the text has
  moved. A pattern that stops matching raises nothing, so it is caught instead by
  coverage — each document declares what it must yield and is stored
  `extraction_incomplete`, with reasons, when it does not. Only the annual-report
  extractor declares real requirements so far; the other three fall back to
  "produced at least one claim", which proves very little.
- An extractor fix reaches already-ingested documents: the pipeline compares
  each document's stored `extractor_version` against the version shipped and
  re-extracts on a mismatch (`--reprocess` forces it). Claims are replaced, not
  stacked; `extraction_run` keeps the version history that replacement would
  erase, and conflicts keep `first_detected_at` because they are upserted on
  the fact key rather than rebuilt from claim ids.
- Only ICRA has ≥2 reports here, so only ICRA produces diffs.
- Rationale bullets are diffed on their headline text, so a reworded strength
  reads as one removal plus one addition.
- Both numeric tolerances (0.10% and ₹5 crore) are single declared constants
  in `reconcile.py`, not a per-fact policy.
- CARE and Brickwork publish no instrument-level maturity table, so the maturity
  ladder comes from ICRA's Annexure I only.
