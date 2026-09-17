"""Cross-document reconciliation.

The system never picks a winner. When sources disagree it writes a conflict row
and links every participating claim, each of which carries its own evidence.

Two guards define what counts as a disagreement, and both matter:

  * more than one *document* — otherwise a single rating action that assigns
    different grades to different instruments ("CRISIL AA / CRISIL AA- /
    CRISIL PPMLD AA" in one press release) reads as self-contradiction.
  * more than one *source* — one agency revising its own view over time is a
    rating action, which belongs in the change feed, not the conflict feed.

Conflicts are durable. They are upserted, never deleted: first_detected_at
answers "when did this appear", last_seen_at "is it still true", and resolved_at
records a disagreement that stopped recurring — itself worth knowing.

Ratings are compared on effective-dated intervals, not calendar quarters. See
effective.py: a rating stands from its action date until the same agency next
acts on the same instrument class, and only agencies whose intervals overlap —
on the same instrument class and the same rating scale — can be said to
disagree. The quarter buckets this replaces both merged an agency's successive
actions that landed in one quarter and split concurrent views that straddled a
boundary.
"""
from __future__ import annotations

from decimal import Decimal

from .effective import build_rating_states, concurrent_pairs
from .models import normalize_value

# Agreement is tested two ways, and failing EITHER makes it a conflict.
#
# A relative band alone mislabels the same discrepancy differently depending on
# the size of the base it sits on. IIFL's FY2024 figures are the worked example:
# Brickwork's total debt exceeds the annual report's by ₹24.80 crore on a
# consolidated basis (0.053%) and ₹25.10 crore standalone (0.126%). Almost
# certainly one definitional difference, but a percentage-only test flags the
# second and waves the first through.
#
# So: a relative band for genuine rounding, and an absolute floor below which a
# rupee difference is too small to be worth an analyst's time regardless of base.
TOLERANCE_PCT = Decimal("0.10")
TOLERANCE_ABS_CR = Decimal("5")          # ₹5 crore
ABSOLUTE_UNITS = ("INR_CRORE",)          # the floor is meaningless for ratios/percentages

# Every fact_key is reconciled unless it is listed here. An allowlist silently
# drops each new extractor's output; this way a new fact type is compared by
# default and an exclusion has to be argued for in writing.
EXCLUDED_KEYS: tuple[tuple[str, str], ...] = (
    ("rated_amount|",
     "the key embeds the agency, and a rated amount measures how much of a "
     "programme that agency was asked to rate — not a shared quantity"),
    ("instrument|",
     "the key embeds the amount itself, so any cross-document match is "
     "tautological agreement rather than independent corroboration"),
)


def _excluded(fact_key: str) -> str | None:
    for prefix, reason in EXCLUDED_KEYS:
        if fact_key.startswith(prefix):
            return reason
    return None


def _members(conn, issuer_id: int, fact_key: str) -> list[dict]:
    # fact_key is unique only within an issuer: 'rating_outlook|long_term|2025Q3'
    # is a key every rated company has. Filtering on it alone would build
    # conflicts out of two different companies' ratings.
    return conn.execute(
        """
        SELECT c.id, c.value_numeric, c.value_unit, c.value_text, c.normalized_value,
               c.subject, c.document_id, d.source_name, d.published_date
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.issuer_id = %s AND c.fact_key = %s
        ORDER BY d.published_date NULLS LAST, c.id
        """,
        (issuer_id, fact_key),
    ).fetchall()


def _comparable_keys(conn, issuer_id: int, column: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Fact keys held by >1 document AND >1 source. Returns (kept, skipped)."""
    rows = conn.execute(
        f"""
        SELECT c.fact_key FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.issuer_id = %s AND c.{column} IS NOT NULL
        GROUP BY c.fact_key
        HAVING count(DISTINCT c.document_id) > 1
           AND count(DISTINCT d.source_name) > 1
        ORDER BY c.fact_key
        """,
        (issuer_id,),
    ).fetchall()

    kept, skipped = [], []
    for row in rows:
        reason = _excluded(row["fact_key"])
        (skipped.append((row["fact_key"], reason)) if reason else kept.append(row["fact_key"]))
    return kept, skipped


def _record(conn, issuer_id: int, fact_key: str, subject: str, kind: str,
            spread_pct: Decimal | None, note: str, rows: list[dict],
            ended_on=None) -> int | None:
    """Upsert a conflict. Returns None when the rows do not span two sources.

    `ended_on` is the date the sources say the disagreement stopped — the end
    of a rating window — as distinct from resolved_at, which is the pipeline
    no longer detecting it. A historical interval is re-detected on every run,
    so without this column it would read as open forever.
    """
    if len({r["source_name"] for r in rows}) < 2:
        return None

    row = conn.execute(
        """
        INSERT INTO conflict (issuer_id, fact_key, subject, kind, tolerance_pct,
                              spread_pct, note, ended_on)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (issuer_id, fact_key, kind)
        DO UPDATE SET subject      = EXCLUDED.subject,
                      spread_pct   = EXCLUDED.spread_pct,
                      note         = EXCLUDED.note,
                      ended_on     = EXCLUDED.ended_on,
                      last_seen_at = now(),
                      resolved_at  = NULL      -- it came back; it is open again
        RETURNING id
        """,
        (issuer_id, fact_key, subject, kind, TOLERANCE_PCT, spread_pct, note, ended_on),
    ).fetchone()

    # Claim ids are rewritten whenever a document is re-extracted, so membership
    # is refreshed while the conflict's own identity and history survive.
    conn.execute("DELETE FROM conflict_member WHERE conflict_id = %s", (row["id"],))
    for member in rows:
        conn.execute(
            "INSERT INTO conflict_member (conflict_id, claim_id, stated_value) "
            "VALUES (%s,%s,%s) ON CONFLICT (conflict_id, claim_id) "
            "DO UPDATE SET stated_value = EXCLUDED.stated_value",
            (row["id"], member["id"], member.get("stated_value")),
        )
    return row["id"]


def disagrees(lo: Decimal, hi: Decimal, unit: str | None) -> tuple[bool, Decimal, str]:
    """Apply both tolerances. Returns (is_conflict, spread_pct, why)."""
    gap = hi - lo
    spread = (gap / lo * 100) if lo else Decimal(0)
    over_pct = spread > TOLERANCE_PCT
    over_abs = unit in ABSOLUTE_UNITS and gap > TOLERANCE_ABS_CR

    if over_pct and over_abs:
        why = (f"₹{gap} crore ({spread:.3f}%) — over both the {TOLERANCE_PCT}% "
               f"and ₹{TOLERANCE_ABS_CR} crore tolerances")
    elif over_pct:
        why = (f"₹{gap} crore ({spread:.3f}%) — above the {TOLERANCE_PCT}% tolerance")
    elif over_abs:
        why = (f"₹{gap} crore — above the ₹{TOLERANCE_ABS_CR} crore tolerance, "
               f"though only {spread:.3f}% of the reported figure")
    else:
        why = f"₹{gap} crore ({spread:.3f}%) — within both tolerances"
    return over_pct or over_abs, spread, why


def _describe(rows: list[dict]) -> str:
    """Group values by source. A source may legitimately hold more than one."""
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["source_name"], []).append(row["value_text"])
    return "; ".join(f"{source} says {' / '.join(values)}"
                     for source, values in sorted(by_source.items()))


CLASS_LABELS = {
    "ncd": "non-convertible debentures",
    "subordinated_debt": "subordinated debt",
    "perpetual_debt": "perpetual debt",
    "bank_facility": "bank facilities",
    "mld": "market-linked debentures",
    "commercial_paper": "commercial paper",
    "debt_securities": "debt securities",
    "other": "other instruments",
}

# What two concurrent ratings of the same instrument can disagree about.
RATING_ASPECTS = (("grade", "rating_grade", "grade"),
                  ("outlook", "rating_outlook", "outlook"),
                  ("watch", "rating_watch", "watch"))


def _window_text(start, end) -> str:
    return f"from {start}" + (f" to {end}" if end else " (still in force)")


def _merge_runs(windows: list[dict]) -> list[dict]:
    """Collapse consecutive windows of the same disagreement into one run.

    Each of an agency's rating actions opens a new interval, so an unchanged
    disagreement — Brickwork AA+ against ICRA AA on debentures — would otherwise
    be re-detected as a fresh conflict every time either agency reaffirms. That
    is the same defect the quarter buckets had, wearing a better hat: a
    disagreement that has persisted since September is one fact about the
    issuer, not three.

    Windows are already grouped by aspect, instrument class, scale and the exact
    pair of values, so merging is purely temporal: contiguous or overlapping
    intervals become one, and a gap (someone agreed for a while) correctly
    starts a new run.
    """
    ordered = sorted(windows, key=lambda w: w["start"])
    runs: list[dict] = []
    for window in ordered:
        current = runs[-1] if runs else None
        contiguous = (current is not None
                      and (current["end"] is None or window["start"] <= current["end"]))
        if not contiguous:
            runs.append({**window, "members": dict(window["members"])})
            continue
        current["members"].update(window["members"])
        if current["end"] is not None:
            current["end"] = (None if window["end"] is None
                              else max(current["end"], window["end"]))
    return runs


def _rating_conflicts(conn, issuer_id: int, seen: list[int]) -> int:
    """Disagreements between agencies whose ratings were in force together.

    A conflict's identity is (aspect, instrument class, scale, start of the run).
    Keying on the *start* is what makes the history durable: as the disagreement
    extends forward the key does not move, so first_detected_at survives and
    last_seen_at advances, which is the question an analyst actually asks —
    how long have these two disagreed?
    """
    build_rating_states(conn, issuer_id)

    grouped: dict[tuple, list[dict]] = {}
    for instrument_class, term, a, b, (start, end) in concurrent_pairs(conn, issuer_id):
        for field, prefix, label in RATING_ASPECTS:
            left, right = a[field], b[field]
            if left is None or right is None:
                # One agency not assigning an outlook is not a counter-opinion.
                continue
            if normalize_value(left) == normalize_value(right):
                continue
            stated = tuple(sorted(((a["agency"], left, a["instrument"]),
                                   (b["agency"], right, b["instrument"]))))
            # The pair of agencies is part of the identity. CARE disagreeing
            # with Brickwork about debentures and ICRA disagreeing with
            # Brickwork about debentures are two disagreements, and collapsing
            # them onto one key would hide whichever was recorded second.
            pair = "~".join(agency for agency, _, _ in stated)
            key = (prefix, label, instrument_class, term, pair,
                   tuple((agency, normalize_value(value)) for agency, value, _ in stated))
            grouped.setdefault(key, []).append({
                "start": start, "end": end, "stated": stated,
                "members": {a["claim_id"]: a["agency"], b["claim_id"]: b["agency"]},
            })

    found = 0
    for (prefix, label, instrument_class, term, pair, _values), windows in sorted(
            grouped.items(), key=lambda item: str(item[0])):
        for run in _merge_runs(windows):
            said = {agency: value for agency, value, _ in run["stated"]}
            rows = [{"id": claim_id, "source_name": agency,
                     "stated_value": said.get(agency)}
                    for claim_id, agency in sorted(run["members"].items())]
            scale = "Long-term" if term == "long_term" else "Short-term"
            what = CLASS_LABELS.get(instrument_class, instrument_class)
            subject = f"{scale} {label} on {what}, {_window_text(run['start'], run['end'])}"
            note = " · ".join(f"{agency} says {value} ({instrument})"
                              for agency, value, instrument in run["stated"])
            conflict_id = _record(
                conn, issuer_id,
                f"{prefix}|{instrument_class}|{term}|{pair}|{run['start'].isoformat()}",
                subject, "categorical_disagreement", None,
                f"{note} — both in force {_window_text(run['start'], run['end'])}", rows,
                ended_on=run["end"])
            if conflict_id:
                found += 1
                seen.append(conflict_id)
    return found


def reconcile(conn, issuer_id: int) -> dict:
    """Detect conflicts. Returns a summary including corroborations."""
    conflicts, corroborations = 0, 0
    seen: list[int] = []

    numeric_keys, numeric_skipped = _comparable_keys(conn, issuer_id, "value_numeric")
    for fact_key in numeric_keys:
        rows = [r for r in _members(conn, issuer_id, fact_key)
                if r["value_numeric"] is not None]
        values = [r["value_numeric"] for r in rows]
        lo, hi = min(values), max(values)
        unit = rows[0]["value_unit"]
        sources = sorted({r["source_name"] for r in rows})
        is_conflict, spread, why = disagrees(lo, hi, unit)

        if is_conflict:
            conflict_id = _record(
                conn, issuer_id, fact_key, rows[0]["subject"], "numeric_disagreement", spread,
                f"{' vs '.join(sources)} differ by {why}", rows)
            if conflict_id:
                conflicts += 1
                seen.append(conflict_id)
        else:
            corroborations += 1

    text_keys, text_skipped = _comparable_keys(conn, issuer_id, "normalized_value")
    for fact_key in text_keys:
        rows = [r for r in _members(conn, issuer_id, fact_key)
                if r["normalized_value"] is not None]
        if len({r["normalized_value"] for r in rows}) < 2:
            continue        # same value written differently is not a disagreement
        conflict_id = _record(conn, issuer_id, fact_key, rows[0]["subject"],
                              "categorical_disagreement", None, _describe(rows), rows)
        if conflict_id:
            conflicts += 1
            seen.append(conflict_id)

    conflicts += _rating_conflicts(conn, issuer_id, seen)

    # A conflict that stops recurring is stamped, not deleted.
    resolved = conn.execute(
        """
        UPDATE conflict SET resolved_at = now()
        WHERE issuer_id = %s AND resolved_at IS NULL AND NOT (id = ANY(%s))
        RETURNING id
        """,
        (issuer_id, seen),
    ).fetchall()

    return {
        "conflicts": conflicts,
        "corroborations": corroborations,
        "resolved": len(resolved),
        "excluded_keys": numeric_skipped + text_skipped,
    }
