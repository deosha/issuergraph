# IssuerGraph — vertical slice schema

Minimum model required by the acceptance test:

> open IIFL Finance → see a debt/rating fact → click it → see the exact source
> evidence → see when another document disagrees with it.

Nine tables. Nothing speculative: no scores, no embeddings, no tenancy.

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
- No `confidence` float. A claim is either anchored or it does not exist.

### `evidence_anchor`
Where the claim came from. N anchors per claim (a debt figure may be supported
by the label cell *and* the amount cell). `ordinal` orders them.

### `rating_action`
Rating-specific projection of a claim: agency, instrument, rated amount, rating,
outlook, watch, action verb, and the previous rating where the document states
it. Joined 1:1 to its `claim`, so it inherits the claim's evidence.

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

Claim ids are rewritten whenever a document is re-extracted, so each run
refreshes `conflict_member` while the conflict's own identity and timestamps
survive.

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
