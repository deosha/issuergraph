# IssuerGraph

Issuer debt and rating changes, with the source behind every figure.

One issuer, six real public documents, end to end. Every extracted fact carries a
character-exact anchor into the page it came from; disagreements between sources
become conflict rows rather than a silently chosen number.

## The acceptance test

> open IIFL Finance → see a debt/rating fact → click it → see the exact source
> evidence → see when another document disagrees with it.

`tests/test_evidence.py` asserts this end to end; `test_reconcile_fixes.py`,
`test_extraction_coverage.py` and `test_api_scope.py` lock down the loader,
reconciliation, extraction coverage, declared units and issuer scoping
(178 tests total, including the public site).

## Run it

```bash
brew services start postgresql@14
createdb issuergraph
psql -d issuergraph -f sql/schema.sql
# existing databases only — schema.sql already includes both:
psql -d issuergraph -f sql/002_conflict_history.sql
psql -d issuergraph -f sql/003_extraction_coverage.sql
psql -d issuergraph -f sql/004_extraction_runs.sql
psql -d issuergraph -f sql/005_rating_states.sql
psql -d issuergraph -f sql/006_pilot_requests.sql
psql -d issuergraph -f sql/007_withdrawn_and_ended.sql
psql -d issuergraph -f sql/008_gradeless_withdrawals.sql
psql -d issuergraph -f sql/009_rating_history.sql
psql -d issuergraph -f sql/010_html_evidence.sql
psql -d issuergraph -f sql/011_agency_adjusted.sql
psql -d issuergraph -f sql/012_gap_materiality_and_source_drift.sql

uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python fastapi 'uvicorn[standard]' 'psycopg[binary]' \
    pydantic pymupdf python-dateutil pytest

.venv/bin/python -m issuergraph.pipeline --reset     # fetch → ingest → extract → reconcile → diff
.venv/bin/python -m pytest tests -q
.venv/bin/uvicorn issuergraph.api:app --port 8077    # open http://localhost:8077
```

| Route | What it is | Needs a database? |
|---|---|---|
| `/` | Landing page | no |
| `/demo` | Interactive demo over a committed snapshot | no |
| `/pilot` | Pilot request form | yes, for storage |
| `/app` | The live workspace over the pipeline's data | yes |

The demo is served from `static/demo/snapshot.json` plus the page images beside
it, so a public deployment needs neither the PDFs nor the document database.
Re-export it with `python -m scripts.export_demo`; the exporter verifies every
anchor against the live page text before writing. Set `ISSUERGRAPH_DEMO_ONLY=1`
to serve the site and demo and 404 `/app`. Configuration is in `.env.example`
and deployment in `docs/DEPLOY.md`.

`ISSUERGRAPH_DSN` overrides the default `postgresql:///issuergraph`. Everything
else the site needs — contact details, optional analytics, optional CRM
forwarding, pilot pricing — is optional and documented in `.env.example`.

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

**314 claims** extracted, every one anchored.

## What it found

Seven conflicts, all real, all like-for-like:

| Fact | Disagreement |
|---|---|
| `rating_grade\|ncd\|long_term\|Brickwork~ICRA` | Brickwork **AA+**, ICRA **AA** on debentures — one notch, continuously since 15 Sep 2025 |
| `rating_grade\|ncd\|long_term\|Brickwork~CARE` | Brickwork **AA+**, CARE **AA** on debentures — CARE's last action was Mar 2024 and still stands |
| `rating_outlook\|ncd\|long_term\|Brickwork~ICRA` | Brickwork **Stable**, ICRA **Negative** on debentures, since ICRA's action of 24 Sep 2025 |
| `rating_watch\|ncd\|long_term\|CARE~ICRA` | 12 Mar 2024, same day: ICRA **Negative** Implications, CARE **Developing** — on debentures |
| `rating_watch\|bank_facility\|long_term\|CARE~ICRA` | the same split, on bank facilities — a second instrument, so a second conflict |
| `total_borrowings\|standalone\|2024-03-31` | Brickwork ₹20,011 Cr vs annual report ₹19,985.90 Cr — ₹25.10 Cr, 0.126% |
| `total_borrowings\|consolidated\|2024-03-31` | Brickwork ₹46,699 Cr vs annual report ₹46,674.20 Cr — ₹24.80 Cr, 0.053% |

Each rating conflict names one instrument class, one rating scale, one pair of
agencies and the period during which both views were in force. The two watch
rows are the same disagreement about two different instruments, which is two
facts, not one: an earlier version compared CARE's *bank facilities* against
ICRA's *debenture programme* and reported a single conflict.

The two numeric ones are a single definitional difference in Brickwork's "Total Debt". They
are caught only because agreement is tested two ways: a relative band (0.10%)
*and* an absolute floor (₹5 crore). A percentage-only test flags the standalone
gap and passes the consolidated one, which is ₹0.30 crore smaller.

Two facts independently corroborated (consolidated and standalone FY25 total
borrowings, residual variance ₹0.03 Cr and ₹0.16 Cr — reported rather than
rendered as an exact match).

Seventeen tracked changes between successive ICRA reports, including the Sept 2025 →
Feb 2026 liquidity narrative moving from ₹3,791 Cr unencumbered cash (Jul 2025)
to ₹5,971 Cr (Dec 2025), and the Mar 2024 → Sep 2025 transition from a rating
watch to a Negative outlook.

## Design

Read `docs/SCHEMA.md` first — it explains the nine tables and the invariants.

Six things are worth calling out:

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

**Two agencies disagree only about the same instrument, at the same time.**
Ratings used to be compared inside calendar quarters, on a value taken from
whichever row of the agency's table was read first — so one agency's view of
bank facilities could be reported as contradicting another's view of debentures,
and two agencies rating five days apart across a quarter boundary never met. Now
a rating stands from its action date until the same agency next acts on the same
instrument class (`effective.py`), and a conflict needs an overlap of those
periods, the same instrument class and the same rating scale. Consecutive
windows of an unchanged disagreement merge into one run, so reaffirming a
difference does not mint a new conflict.

## Layout

```
sql/schema.sql              nine tables
sql/002..012_*.sql          conflict history; coverage; reprocessing;
                            rating states; pilot requests; withdrawals + ended;
                            gradeless withdrawals; rating history + corpus gaps;
                            HTML evidence; agency-adjusted basis;
                            gap materiality; source drift
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
  effective.py              rating timelines: what each agency says, and when
  reconcile.py              conflict detection (0.10% and ₹5 crore tolerances)
  diff.py                   rationale-to-rationale diff
  corpus.py                 the six sources, declared
  pipeline.py               the ordered run
  api.py                    read-only API + highlighted page renderer
static/index.html           landing page
static/demo.html            the demo (snapshot-backed)
static/pilot.html           pilot request form
static/app.html             the live workspace
static/app.js, app.css      the product UI, shared by /app and /demo
static/demo/               committed snapshot + rendered page images
issuergraph/site.py         public routes (pages, demo API, pilot endpoint)
issuergraph/demo.py         the snapshot, and the guided tour built from it
issuergraph/pilot.py        validate, throttle, store, optionally forward
issuergraph/settings.py     environment-backed configuration
scripts/export_demo.py      reproducible demo snapshot export
scripts/tryextract.py       dry-run an extractor against a PDF, no database
```

## Deliberately absent

No score, no recommendation, no chat, no vector store, no agent framework, no
queue, no auth, no tenancy. Orchestration is a function that runs in order.

## Known limits of this slice

- Five extractors, tuned to five publishers' current layouts (Crisil's is HTML,
  anchored by node path). A format change is
  caught two ways, and an earlier version of this README overstated the first:
  offset assertions fail loudly only when a pattern *matches* and the text has
  moved. A pattern that stops matching raises nothing, so it is caught instead by
  coverage — each document declares what it must yield and is stored
  `extraction_incomplete`, with reasons, when it does not. Every extractor now
  declares per-section requirements (the rating table, liquidity, sensitivities
  and rationale bullets for the agencies; an ICRA material-event release
  declares only the table), and a change-feed entry whose absent side comes
  from an incomplete report is marked `unconfirmed`.
- An extractor fix reaches already-ingested documents: the pipeline compares
  each document's stored `extractor_version` against the version shipped and
  re-extracts on a mismatch (`--reprocess` forces it). Claims are replaced, not
  stacked; `extraction_run` keeps the version history that replacement would
  erase, and conflicts keep `first_detected_at` because they are upserted on
  the fact key rather than rebuilt from claim ids.
- ICRA and CARE each have three reports here, so they produce diffs; Brickwork
  has one and produces none.
- A rating is treated as in force until the same agency acts again or withdraws
  it on that tranche. "Acts again" includes actions we know of only from the
  agency's own rating-history annexure: each rationale's history table is read
  as anchored `rating_history` claims, and an action it lists with no primary
  rationale to match is a `corpus_gap` that ends the earlier period, opens one
  of its own, and flags conflicts across it `incomplete_corpus` (see
  `completeness.py`). For IIFL that finds one missing document, ICRA's
  25 September 2024 action; the demo export refuses to publish the three
  conflicts it flags without `--allow-gaps`.
- The annexures can only list actions up to the agency's latest document we
  hold. Anything an agency did after that is invisible — CARE's AA-/Stable of
  September 2024 is the last CARE action we can see, and there is still no
  staleness horizon: it counts as current. A maximum age per agency remains the
  missing piece, and a separate decision.
- ICRA's Feb 2026 rationale carries no market-linked-debenture rating, so the
  change feed reports one removed — confirmed, because that report met its
  coverage declaration, and consistent with the Sept 2025 rows being
  "reaffirmed and withdrawn".
- Rationale bullets are diffed on their headline text, so a reworded strength
  reads as one removal plus one addition.
- Both numeric tolerances (0.10% and ₹5 crore) are single declared constants
  in `reconcile.py`, not a per-fact policy.
- CARE and Brickwork publish no instrument-level maturity table, so the maturity
  ladder comes from ICRA's Annexure I only.
