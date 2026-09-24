"""Rationale-to-rationale diff.

Compares successive rationales from the *same* agency. Structural only: what
appeared, what disappeared, what changed value. No sentiment scoring — the
analyst reads both original texts, each anchored to its page.
"""
from __future__ import annotations

import re

QUARTER_SUFFIX = re.compile(r"\|\d{4}Q\d$")

# Fact keys carrying a single tracked value per report.
TRACKED_PREFIXES = ("liquidity_assessment|", "liquidity_detail|", "rating_grade|",
                    "rating_outlook|", "rating_watch|", "rating_sensitivity|")
SET_PREFIXES = ("rationale|",)

SECTION_OF = {
    "liquidity_assessment": "liquidity",
    "liquidity_detail": "liquidity",
    "rating_grade": "rating",
    "rating_outlook": "rating",
    "rating_watch": "rating",
    "rating_sensitivity": "sensitivities",
}


def _diff_key(fact_key: str) -> str:
    return QUARTER_SUFFIX.sub("", fact_key)


def _section(fact_key: str) -> str:
    head = fact_key.split("|", 1)[0]
    if head == "rationale":
        return fact_key.split("|")[2]      # strengths / weaknesses
    return SECTION_OF.get(head, head)


def _rating_rows(conn, document_id: int) -> dict[str, dict]:
    """One agency's rating of one instrument class, keyed so reports line up.

    The rating claims themselves are keyed per tranche, with the rated amount in
    the key, so they never match across two reports of the same programme. The
    comparable identity is the instrument class and the rating scale — the same
    identity reconciliation uses — so the change feed is built from that rather
    than from fact keys.

    A class rated inconsistently within one report has no single value to track,
    so it is left out of the diff instead of being represented by whichever row
    was read first. That guess is what this work exists to remove.
    """
    rows = conn.execute(
        """
        SELECT c.id, r.instrument_class, r.term, r.rating, r.outlook, r.watch
        FROM rating_action r JOIN claim c ON c.id = r.claim_id
        WHERE c.document_id = %s
        ORDER BY c.id
        """,
        (document_id,),
    ).fetchall()

    tracked: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for row in rows:
        for field, prefix in (("rating", "rating_grade"), ("outlook", "rating_outlook"),
                              ("watch", "rating_watch")):
            if row[field] is None:
                continue
            key = f"{prefix}|{row['instrument_class']}|{row['term']}"
            existing = tracked.get(key)
            if existing is None:
                tracked[key] = {"id": row["id"], "value_text": row[field],
                                "subject": f"{row['instrument_class']} ({row['term']})"}
            elif existing["value_text"] != row[field]:
                ambiguous.add(key)
    for key in ambiguous:
        tracked.pop(key, None)
    return tracked


def _claims_for(conn, document_id: int) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT id, fact_key, value_text, subject FROM claim
        WHERE document_id = %s AND value_text IS NOT NULL
          AND (fact_key LIKE 'liquidity_assessment|%%' OR fact_key LIKE 'liquidity_detail|%%' OR fact_key LIKE 'rating_grade|%%'
               OR fact_key LIKE 'rating_outlook|%%' OR fact_key LIKE 'rating_watch|%%'
               OR fact_key LIKE 'rating_sensitivity|%%' OR fact_key LIKE 'rationale|%%')
        ORDER BY id
        """,
        (document_id,),
    ).fetchall()
    return {**{_diff_key(r["fact_key"]): r for r in rows},
            **_rating_rows(conn, document_id)}


def build_diffs(conn, issuer_id: int) -> int:
    conn.execute("DELETE FROM rationale_diff WHERE issuer_id = %s", (issuer_id,))

    agencies = conn.execute(
        """
        SELECT DISTINCT source_name FROM document
        WHERE issuer_id = %s AND doc_type = 'rating_rationale' ORDER BY source_name
        """,
        (issuer_id,),
    ).fetchall()

    written = 0
    for agency in agencies:
        docs = conn.execute(
            """
            SELECT id, published_date, extraction_status FROM document
            WHERE issuer_id = %s AND doc_type = 'rating_rationale' AND source_name = %s
            ORDER BY published_date NULLS FIRST, id
            """,
            (issuer_id, agency["source_name"]),
        ).fetchall()
        if len(docs) < 2:
            continue

        for older, newer in zip(docs, docs[1:]):
            before = _claims_for(conn, older["id"])
            after = _claims_for(conn, newer["id"])

            for key in sorted(set(before) | set(after)):
                old, new = before.get(key), after.get(key)
                if old and new and old["value_text"] == new["value_text"]:
                    continue
                # An absence is only a change if the report it is absent from
                # was read in full. A report that failed its coverage
                # declaration may simply not have been parsed there, and
                # "dropped from the later report" is then a claim the data
                # cannot support.
                if old and new:
                    direction, lacking = "changed", None
                elif new:
                    direction, lacking = "added", older
                else:
                    direction, lacking = "removed", newer
                certainty = ("unconfirmed"
                             if lacking is not None
                             and lacking["extraction_status"] != "complete"
                             else "confirmed")

                conn.execute(
                    """
                    INSERT INTO rationale_diff
                        (issuer_id, agency, from_document_id, to_document_id, from_date, to_date,
                         section, direction, certainty, from_claim_id, to_claim_id,
                         from_text, to_text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (issuer_id, agency["source_name"], older["id"], newer["id"],
                     older["published_date"], newer["published_date"], _section(key), direction,
                     certainty,
                     old["id"] if old else None, new["id"] if new else None,
                     old["value_text"] if old else None, new["value_text"] if new else None),
                )
                written += 1

            # A publisher restating a figure it printed before (Brickwork's
            # total debt appears in each of its rationales): the same value is
            # not news; a different value for the same basis and date is.
            for key, old, new in _restatements(conn, older["id"], newer["id"]):
                conn.execute(
                    """
                    INSERT INTO rationale_diff
                        (issuer_id, agency, from_document_id, to_document_id, from_date, to_date,
                         section, direction, certainty, from_claim_id, to_claim_id,
                         from_text, to_text)
                    VALUES (%s,%s,%s,%s,%s,%s,'total debt','changed','confirmed',%s,%s,%s,%s)
                    """,
                    (issuer_id, agency["source_name"], older["id"], newer["id"],
                     older["published_date"], newer["published_date"], old["id"], new["id"],
                     f"{old['subject']}: {old['value_numeric']}",
                     f"{new['subject']}: {new['value_numeric']}"),
                )
                written += 1
    return written


def _restatements(conn, older_id: int, newer_id: int):
    """Figures both reports state for the same basis and date, with different values."""
    rows = conn.execute(
        """
        SELECT o.fact_key, o.id AS old_id, o.value_numeric AS old_value, o.subject AS old_subject,
               n.id AS new_id, n.value_numeric AS new_value, n.subject AS new_subject
        FROM claim o JOIN claim n ON n.fact_key = o.fact_key
        WHERE o.document_id = %s AND n.document_id = %s
          AND o.claim_type = 'total_borrowings' AND n.claim_type = 'total_borrowings'
          AND o.value_numeric IS DISTINCT FROM n.value_numeric
        """,
        (older_id, newer_id),
    ).fetchall()
    for r in rows:
        yield (r["fact_key"],
               {"id": r["old_id"], "value_numeric": r["old_value"], "subject": r["old_subject"]},
               {"id": r["new_id"], "value_numeric": r["new_value"], "subject": r["new_subject"]})
