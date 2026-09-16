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

TODO(effective-dating): rating fact keys bucket by calendar quarter
(rating_grade|long_term|2025Q3). That both merges actions that merely landed in
the same quarter and splits genuinely concurrent views that straddle a quarter
boundary. It needs replacing with effective-dated intervals per
(agency, instrument) — an action is in force until the next action supersedes
it — which is a separate piece of work, not a tweak here.
"""
from __future__ import annotations

from decimal import Decimal

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
            spread_pct: Decimal | None, note: str, rows: list[dict]) -> int | None:
    """Upsert a conflict. Returns None when the rows do not span two sources."""
    if len({r["source_name"] for r in rows}) < 2:
        return None

    row = conn.execute(
        """
        INSERT INTO conflict (issuer_id, fact_key, subject, kind, tolerance_pct,
                              spread_pct, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (issuer_id, fact_key, kind)
        DO UPDATE SET subject      = EXCLUDED.subject,
                      spread_pct   = EXCLUDED.spread_pct,
                      note         = EXCLUDED.note,
                      last_seen_at = now(),
                      resolved_at  = NULL      -- it came back; it is open again
        RETURNING id
        """,
        (issuer_id, fact_key, subject, kind, TOLERANCE_PCT, spread_pct, note),
    ).fetchone()

    # Claim ids are rewritten whenever a document is re-extracted, so membership
    # is refreshed while the conflict's own identity and history survive.
    conn.execute("DELETE FROM conflict_member WHERE conflict_id = %s", (row["id"],))
    for member in rows:
        conn.execute(
            "INSERT INTO conflict_member (conflict_id, claim_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (row["id"], member["id"]),
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
