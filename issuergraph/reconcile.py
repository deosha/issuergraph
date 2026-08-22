"""Cross-document reconciliation.

The system never picks a winner. When sources disagree it writes a conflict row
and links every participating claim, each of which carries its own evidence.
"""
from __future__ import annotations

from decimal import Decimal

# Two figures for the same fact are treated as agreeing within this band.
# 0.10% of reported borrowings is roughly a rounding artefact on a ₹50,000 crore
# balance sheet; anything wider is a real difference an analyst must explain.
TOLERANCE_PCT = Decimal("0.10")

NUMERIC_KEYS = ("total_borrowings|",)
CATEGORICAL_KEYS = ("rating_grade|", "rating_outlook|", "rating_watch|")


def _members(conn, fact_key: str) -> list[dict]:
    return conn.execute(
        """
        SELECT c.id, c.value_numeric, c.value_text, c.subject, c.document_id,
               d.source_name, d.published_date
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.fact_key = %s
        ORDER BY d.published_date NULLS LAST, c.id
        """,
        (fact_key,),
    ).fetchall()


def _record(conn, issuer_id: int, fact_key: str, subject: str, kind: str,
            spread_pct: Decimal | None, note: str, claim_ids: list[int]) -> None:
    row = conn.execute(
        """
        INSERT INTO conflict (issuer_id, fact_key, subject, kind, tolerance_pct, spread_pct, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (issuer_id, fact_key, kind)
        DO UPDATE SET spread_pct = EXCLUDED.spread_pct, note = EXCLUDED.note,
                      detected_at = now()
        RETURNING id
        """,
        (issuer_id, fact_key, subject, kind, TOLERANCE_PCT, spread_pct, note),
    ).fetchone()
    for claim_id in claim_ids:
        conn.execute(
            "INSERT INTO conflict_member (conflict_id, claim_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (row["id"], claim_id),
        )


def reconcile(conn, issuer_id: int) -> dict:
    """Detect conflicts. Returns a summary including corroborations."""
    conn.execute("DELETE FROM conflict WHERE issuer_id = %s", (issuer_id,))
    conflicts, corroborations = 0, 0

    for prefix in NUMERIC_KEYS:
        keys = conn.execute(
            """
            SELECT fact_key, count(DISTINCT document_id) AS docs
            FROM claim WHERE issuer_id = %s AND fact_key LIKE %s AND value_numeric IS NOT NULL
            GROUP BY fact_key HAVING count(DISTINCT document_id) > 1
            """,
            (issuer_id, prefix + "%"),
        ).fetchall()
        for key in keys:
            rows = _members(conn, key["fact_key"])
            values = [r["value_numeric"] for r in rows]
            lo, hi = min(values), max(values)
            spread = (hi - lo) / lo * 100 if lo else Decimal(0)
            sources = sorted({r["source_name"] for r in rows})
            if spread > TOLERANCE_PCT:
                conflicts += 1
                _record(conn, issuer_id, key["fact_key"], rows[0]["subject"],
                        "numeric_disagreement", spread,
                        f"{' vs '.join(sources)} differ by {hi - lo} (₹ crore), "
                        f"{spread:.3f}% — above the {TOLERANCE_PCT}% tolerance",
                        [r["id"] for r in rows])
            else:
                corroborations += 1

    for prefix in CATEGORICAL_KEYS:
        keys = conn.execute(
            """
            SELECT fact_key FROM claim
            WHERE issuer_id = %s AND fact_key LIKE %s AND value_text IS NOT NULL
            GROUP BY fact_key HAVING count(DISTINCT value_text) > 1
            """,
            (issuer_id, prefix + "%"),
        ).fetchall()
        for key in keys:
            rows = _members(conn, key["fact_key"])
            by_source = {r["source_name"]: r["value_text"] for r in rows}
            conflicts += 1
            _record(conn, issuer_id, key["fact_key"], rows[0]["subject"],
                    "categorical_disagreement", None,
                    "; ".join(f"{s} says {v}" for s, v in sorted(by_source.items())),
                    [r["id"] for r in rows])

    return {"conflicts": conflicts, "corroborations": corroborations}
