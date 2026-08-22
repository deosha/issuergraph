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
A conflict is a *first-class row*, not a rendering decision. Created when ≥2
claims share a `fact_key` but their values disagree beyond `tolerance_pct`.
`spread_pct` records how far apart. The system never picks a winner —
`conflict_member` links every participating claim, each with its own evidence.

`kind`:
- `numeric_disagreement` — same fact, different numbers.
- `categorical_disagreement` — same fact, different labels (e.g. two agencies'
  outlooks differ).

### `rationale_diff`
Successive rationales from the same agency for the same issuer. One row per
changed element between report N-1 and report N, with anchors on *both* sides
(`from_claim_id`, `to_claim_id`) so the analyst sees both original texts.
`direction` is `added` / `removed` / `changed` — derived structurally, never
sentiment-scored.

## What is deliberately absent

- No `score`, `recommendation`, or `risk_level` column.
- No vector/embedding column — reconciliation is by explicit `fact_key`.
- No `user`, `tenant`, or `subscription`.
- No job/queue table — the pipeline is a deterministic ordered function.
