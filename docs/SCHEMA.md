# IssuerGraph — vertical slice schema

Minimum model required by the acceptance test:

> open IIFL Finance → see a debt/rating fact → click it → see the exact source
> evidence → see when another document disagrees with it.

Sixteen tables. Nothing speculative: no scores, no embeddings, no tenancy. Nine
carry the evidence graph; the rest exist because of what the first nine could
not say — `extraction_run` (which parser produced these facts), `rating_state`
(when each agency's view was in force), the coverage columns on `document`
(what this document was supposed to yield and did not), and
`rating_history_entry` / `corpus_gap` (which of an agency's own actions we do
not hold a document for).

## Core invariant

**No source = no fact.** A `claim` row is meaningless without at least one
`evidence_anchor`. This is enforced in the loader (`verify_anchors()`), not just
by convention: a claim inserted without an anchor aborts the transaction.

Every anchor carries `document_id`, `page_no`, `char_start`, `char_end`,
`evidence_text`, and `bbox` (always available — see *Page geometry* below).

The loader checks four things, and the whole document loads in one transaction
so a failure rolls it back rather than leaving half its claims behind:

1. the claim has at least one anchor;
2. `0 <= char_start < char_end <= len(page.text)` — checked *before* comparing,
   because Python slicing does not enforce it (`text[99999:99999]` is `""` on
   any page, and a negative `char_start` slices from the end and can
   coincidentally match);
3. `evidence_text` equals that exact slice, and is non-empty;
4. the range resolves to at least one word rectangle, so the anchor can actually
   be drawn — a range landing entirely in inter-word whitespace highlights
   nothing.

## Page geometry

`document_page.text` is **not** `page.get_text()`. It is built deterministically
from PyMuPDF word boxes: words joined by `" "` within a line, lines by `"\n"`.
Each word's `[char_start, char_end, x0, y0, x1, y1]` is stored in
`document_page.word_map`.

Consequence: *any* character range on a page maps back to an exact set of word
rectangles. Highlighting is a lookup, not a text search that may miss. `bbox` on
an anchor is the union rect; `bbox_rects` holds the per-line rects used to draw
the highlight.

## Tables

### `issuer`
The entity being researched. `aliases` carries the name variants that appear in
documents (`IIFL Finance Ltd`, `IIFL`, `India Infoline Finance`).

### `document`
One immutable retrieved artifact. `sha256` is over the raw bytes and is
`UNIQUE` — re-ingesting the same PDF is a no-op, so the corpus is idempotent.
`url` + `retrieved_at` answer "was this source available when the analyst
decided?". `published_date` is the document's own stated date (parsed from its
text), distinct from when we fetched it.

`extraction_status` / `extraction_expected` / `extraction_found` /
`extraction_missing` record whether the extractor found what this document was
declared to contain. Without them, an extractor that stopped matching and an
issuer that stopped disclosing produce identical database state — the claims
that remain are all still perfectly anchored. The CHECK constraint refuses an
`incomplete` status with no stated reasons.

### `extraction_run`
One row per extraction of a document: both extractor versions, why the run
happened (`initial` / `version_changed` / `forced`), and how many claims were
written and replaced. Claims are derived and get rewritten when an extractor is
fixed; this is the history that rewriting would otherwise erase.

### `rating_state`
Effective-dated rating timelines, derived from `rating_action` and rebuilt each
run: one row per (agency, instrument class, rating scale, period), where a
rating stands from its action date until the same agency next acts on the same
class. `effective_to IS NULL` means still in force.

"Next acts" includes actions known only from a rating-history annexure (see
`corpus_gap`): a state ends at the earliest later action from either a primary
rationale or an open within-corpus gap. `end_basis` says which (`primary`,
`history`, `withdrawn`, or null while in force); when it is `history`,
`superseded_by_claim_id` is the history claim that ended it — superseded,
primary document not loaded. A gap also opens a state of its own with
`provenance = 'history_annexure'`, so the agency's view between our documents is
its own annexure's record rather than a blank or the stale earlier view.

This table is what makes cross-agency comparison meaningful. Comparing on the
publisher's own instrument wording cannot work — ICRA's "Non-convertible
debenture programme", CARE's "Non Convertible Debentures" and Brickwork's "NCDs
Public Issue" are one instrument class under three names — and comparing inside
calendar quarters both merges an agency's successive actions and splits
concurrent views that straddle a boundary. A conflict requires overlapping
intervals, one instrument class and one rating scale.

### `document_page`
Per-page canonical text and geometry. PK `(document_id, page_no)`.
`width`/`height`/`rotation` let the UI scale bboxes to any render DPI.

### `claim`
The atomic extracted assertion. One row = one fact from one document.

- `fact_key` — the reconciliation key. Claims sharing a `fact_key` are *about
  the same thing* and are therefore comparable across documents
  (e.g. `total_borrowings|consolidated|2025-03-31`).
- `value_numeric` + `value_unit` for anything comparable; `value_text` for
  qualitative claims (rating outlook, a strength bullet).
- `normalized_value` — `value_text` casefolded with whitespace collapsed,
  written at load time by `normalize_value()`. Reconciliation compares *this*;
  `value_text` stays verbatim for evidence display. Without it "Negative" and
  "NEGATIVE" read as a disagreement.
- `basis` — `standalone` / `consolidated` / `unknown`. Kept **in** the fact_key,
  because a standalone/consolidated mismatch is a category difference, not a
  conflict.
- `extractor` + `extractor_version` — provenance of the *code*, so a bad
  extractor's output can be found and requeried later.
- `provenance` — `primary_rationale` (what a rationale says the agency is doing
  now), `primary_report` (annual report), or `history_annexure` (what a
  rationale's rating-history table says the agency did before). Only a
  `rating_history` claim may be `history_annexure`; the model enforces both
  directions, so a history row is never read as a primary action.
- No `confidence` float. A claim is either anchored or it does not exist.

### `evidence_anchor`
Where the claim came from. N anchors per claim (a debt figure may be supported
by the label cell *and* the amount cell). `ordinal` orders them.

`kind` is `pdf` (`page_no` + offsets into `document_page.text`, with `bbox` /
`bbox_rects` for highlighting) or `html` (`node_path` — lxml `getpath()` on the
parsed raw HTML — + offsets into that node's `text_content()` exactly as lxml
returns it, and no geometry). `evidence_anchor_locator_check` makes each kind
carry only its own locator. An HTML anchor is verified by re-hashing the
document's stored bytes, re-parsing them and resolving the path to exactly one
node.

### `document_blob`
An HTML document's bytes exactly as received, 1:1 with `document`. The loader
verifies HTML anchors against these rather than a local file, after checking
they still hash to `document.sha256`. `document.media_type` says whether a
document is a PDF or HTML, and `document.content_type` keeps the Content-Type
it was served with, whose charset decodes the bytes. Nothing in this table is
ever served: what a publisher's evidence may show is decided by its display
policy (`settings.evidence_policy`, `evidence.py`).

### `rating_action`
Rating-specific projection of a claim: agency, instrument, rated amount, rating,
outlook, watch, action verb, and the previous rating where the document states
it. Joined 1:1 to its `claim`, so it inherits the claim's evidence.

### `rating_history_entry`
History-specific projection of a `rating_history` claim: one row of an agency's
rating-history annexure — agency, the annexure's instrument wording, instrument
class and scale, action date, grade, outlook, watch, withdrawn. Same 1:1-to-claim
rule, so every entry carries anchors (the row's instrument name and the dated
cell). The cell under the document's own action date is that document's primary
action and is not stored as history.

### `corpus_gap` / `corpus_gap_evidence`
A history action with no matching primary action: same agency, class and scale,
action dates within 3 days, same grade (a gradeless withdrawal matches a
primary withdrawal). One gap per (issuer, agency, class, scale, date, grade);
`corpus_gap_evidence` links every history claim that lists it — ICRA's
25 September 2024 action is listed by two later ICRA rationales.

`scope` is `within_corpus` when the gap falls after the agency's earliest
primary action on that class — it truncates a state, opens one, and flags
conflicts across it — and `before_corpus` otherwise: recorded, truncates
nothing. Durable like `conflict`: `first_detected_at` written once,
`last_seen_at` advanced each run, `resolved_at` stamped when the missing
document is loaded. Never deleted.

### `debt_observation`
Debt-specific projection: instrument name/type, amount in ₹ crore, maturity,
secured flag. Same 1:1-to-claim rule.

Both projection tables exist rather than one wide `claim` because their query
shapes differ (rating history is time-ordered per agency; debt is aggregated per
as-of date), and because the type-specific NOT NULLs are real constraints.

### `conflict` / `conflict_member`
A conflict is a *first-class row*, not a rendering decision. The system never
picks a winner — `conflict_member` links every participating claim, each with
its own evidence.

Two guards decide what counts as a disagreement, and a conflict is only recorded
when both hold:

- **more than one document.** Otherwise a single rating action assigning
  different grades to different instruments (CRISIL routinely publishes
  "CRISIL AA / CRISIL AA- / CRISIL PPMLD AA" in one press release) reads as the
  document contradicting itself.
- **more than one source.** One agency revising its own view between reports is
  a rating action; it belongs in `rationale_diff`, not here.

`fact_key` is unique only *within an issuer* — every rated company has a
`rating_outlook|long_term|2025Q3` — so membership queries must filter on
`issuer_id` as well.

`kind`:
- `numeric_disagreement` — same fact, different numbers. Two tolerances apply
  and failing **either** is a conflict: a relative band (`tolerance_pct`, 0.10%)
  for genuine rounding, and an absolute floor (₹5 crore) so the same discrepancy
  is not judged differently depending on the size of its base. IIFL's FY2024
  totals are the worked example — Brickwork exceeds the annual report by
  ₹24.80 crore consolidated (0.053%) and ₹25.10 crore standalone (0.126%),
  almost certainly one definitional difference, which a percentage-only test
  flags in one case and waves through in the other. The floor applies only to
  rupee-denominated units; "6 over tolerance" cannot mean 6 percentage points.
  `spread_pct` records the relative distance either way.
- `categorical_disagreement` — same fact, different labels, compared on
  `claim.normalized_value`.

**History.** Conflicts are upserted, never deleted, because persistence is the
signal:

- `first_detected_at` — written once, never updated. Answers "when did this
  first appear".
- `last_seen_at` — moved forward by every run that still sees it. Answers "is it
  still true".
- `resolved_at` — set when a run no longer detects it, and cleared if it comes
  back. A disagreement that quietly goes away is itself worth knowing about, so
  the row survives.

Claim ids are rewritten whenever a document is re-extracted. Open conflicts
re-record their members each run; the pipeline also carries every member (and
every `corpus_gap_evidence` row) across to the replacement claim that states
the same value, and aborts if none does — otherwise a resolved conflict, which
is not re-recorded, would cascade-lose its evidence.

`incomplete_corpus` is set on a rating conflict when either side is a
history-annexure state, or when either agency has an open within-corpus gap on
that class inside the window. The conflict is kept and shown; the flag says it
rests on a record we know is incomplete. The demo export refuses to publish a
flagged conflict without `--allow-gaps`.

`conflict_member.stated_value` holds what that member said about the disputed
aspect when it is narrower than the claim's own text: the claim quotes the whole
rating line (`[ICRA]AA (Negative); reaffirmed`), while the outlook conflict is
`Negative` against `Stable`. The claim keeps the verbatim evidence; the column
carries the compared value.

### `rationale_diff`
Successive rationales from the same agency for the same issuer. One row per
changed element between report N-1 and report N, with anchors on *both* sides
(`from_claim_id`, `to_claim_id`) so the analyst sees both original texts.
`direction` is `added` / `removed` / `changed` — derived structurally, never
sentiment-scored.

## Why `claim` has no uniqueness constraint

Claims are a pure function of (document, extractor version), so re-extraction
replaces them wholesale: `load_claims` deletes the document's claims first.

The alternative — a unique index on
`(document_id, fact_key, subject, extractor_version)` — would reject real data.
ICRA's instrument annexure legitimately lists the same ISIN twice with identical
issuance date and amount (two tranches of one issue); there are four such rows
in the current corpus.

## What is deliberately absent

- No `score`, `recommendation`, or `risk_level` column.
- No vector/embedding column — reconciliation is by explicit `fact_key`.
- No `user`, `tenant`, or `subscription`.
- No job/queue table — the pipeline is a deterministic ordered function.
