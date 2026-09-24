"""Do we hold every action the agencies say they took?

A rating stands until the same agency next acts on that instrument class — but
"next acts" was only ever "next action we happen to have ingested". CARE's
March 2024 AA therefore read as current after two later CARE actions we did not
hold, and nothing said so. Each agency's rating-history annexure lists its own
past actions with dates; any listed action with no primary rating action to
match is a document we are missing, and is recorded as a corpus gap.

A history entry matches a primary action on the same agency, instrument class
and rating scale when their dates are within MATCH_DAYS of each other and the
grades agree — or, for a withdrawal that states no grade, when the primary
action is a withdrawal. Publication and action dates drift by a day or two
between an agency's release and its own later record of it.

Gaps are durable, like conflicts: first_detected_at is written once,
last_seen_at advances each run, resolved_at is stamped when the gap stops
recurring (its document was loaded). Nothing is deleted.

`scope` separates gaps that matter to what we compute from those that do not.
A gap dated after the agency's earliest primary action on that class falls
inside the period our documents describe: it ends the preceding state early,
opens a state of its own, and flags any disagreement computed across it
('within_corpus'). A gap dated before it precedes everything we hold and
truncates nothing ('before_corpus'); it is still recorded, because "we do not
hold ICRA's 2021 action" is true and worth showing.
"""
from __future__ import annotations

from datetime import date

from .effective import is_withdrawal

MATCH_DAYS = 3


def _history(conn, issuer_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT h.claim_id, h.agency, h.instrument, h.instrument_class, h.term,
               h.action_date, h.grade, h.outlook, h.watch, h.withdrawn,
               c.document_id, d.published_date
        FROM rating_history_entry h
        JOIN claim c ON c.id = h.claim_id
        JOIN document d ON d.id = c.document_id
        WHERE c.issuer_id = %s
        ORDER BY h.agency, h.instrument_class, h.term, h.action_date, h.claim_id
        """,
        (issuer_id,),
    ).fetchall()


def _primary(conn, issuer_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT r.claim_id, r.agency, r.instrument_class, r.term, r.rating AS grade,
               r.action, r.action_date
        FROM rating_action r JOIN claim c ON c.id = r.claim_id
        WHERE c.issuer_id = %s AND r.action_date IS NOT NULL
        """,
        (issuer_id,),
    ).fetchall()


def matches(entry: dict, action: dict) -> bool:
    """Is this primary action the one the history entry records?"""
    if (entry["agency"], entry["instrument_class"], entry["term"]) != \
            (action["agency"], action["instrument_class"], action["term"]):
        return False
    if abs((entry["action_date"] - action["action_date"]).days) > MATCH_DAYS:
        return False
    if entry["grade"] is None:
        return is_withdrawal(action["action"])
    return entry["grade"] == action["grade"]


def find_gaps(history: list[dict], primary: list[dict]) -> list[dict]:
    """Group unmatched history entries into gaps, with every entry as evidence.

    Pure, so the rule can be tested without a database.
    """
    earliest: dict[tuple, date] = {}
    for a in primary:
        key = (a["agency"], a["instrument_class"], a["term"])
        earliest[key] = min(earliest.get(key, a["action_date"]), a["action_date"])

    gaps: dict[tuple, dict] = {}
    for h in history:
        if any(matches(h, a) for a in primary):
            continue
        cls = (h["agency"], h["instrument_class"], h["term"])
        key = (*cls, h["action_date"], h["grade"] or "")
        gap = gaps.setdefault(key, {
            "agency": h["agency"], "instrument_class": h["instrument_class"],
            "term": h["term"], "action_date": h["action_date"], "grade": h["grade"] or "",
            "outlook": h["outlook"], "watch": h["watch"], "withdrawn": h["withdrawn"],
            "scope": ("within_corpus" if cls in earliest and h["action_date"] > earliest[cls]
                      else "before_corpus"),
            "evidence": [],
        })
        gap["evidence"].append(h["claim_id"])
    return list(gaps.values())


def detect_gaps(conn, issuer_id: int) -> dict:
    """Upsert this run's gaps; stamp the ones that stopped recurring."""
    gaps = find_gaps(_history(conn, issuer_id), _primary(conn, issuer_id))
    seen: list[int] = []
    for gap in gaps:
        row = conn.execute(
            """
            INSERT INTO corpus_gap (issuer_id, agency, instrument_class, term, action_date,
                                    grade, outlook, watch, withdrawn, scope)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (issuer_id, agency, instrument_class, term, action_date, grade)
            DO UPDATE SET outlook      = EXCLUDED.outlook,
                          watch        = EXCLUDED.watch,
                          withdrawn    = EXCLUDED.withdrawn,
                          scope        = EXCLUDED.scope,
                          last_seen_at = now(),
                          resolved_at  = NULL
            RETURNING id
            """,
            (issuer_id, gap["agency"], gap["instrument_class"], gap["term"],
             gap["action_date"], gap["grade"], gap["outlook"], gap["watch"],
             gap["withdrawn"], gap["scope"]),
        ).fetchone()
        seen.append(row["id"])
        conn.execute("DELETE FROM corpus_gap_evidence WHERE gap_id = %s", (row["id"],))
        for claim_id in gap["evidence"]:
            conn.execute("INSERT INTO corpus_gap_evidence (gap_id, claim_id) VALUES (%s,%s)",
                         (row["id"], claim_id))

    # A gap whose document has since been loaded is stamped, not deleted.
    resolved = conn.execute(
        """
        UPDATE corpus_gap SET resolved_at = now()
        WHERE issuer_id = %s AND resolved_at IS NULL AND NOT (id = ANY(%s))
        RETURNING id
        """,
        (issuer_id, seen),
    ).fetchall()
    return {"gaps": len(gaps),
            "gaps_within_corpus": sum(g["scope"] == "within_corpus" for g in gaps),
            "gaps_resolved": len(resolved)}


def open_gaps(conn, issuer_id: int, scope: str = "within_corpus") -> list[dict]:
    """Open gaps, each with the history claim that best evidences it: the
    entry from the most recent document listing the action."""
    return conn.execute(
        """
        SELECT g.*, (
            SELECT e.claim_id FROM corpus_gap_evidence e
            JOIN claim c ON c.id = e.claim_id JOIN document d ON d.id = c.document_id
            WHERE e.gap_id = g.id
            ORDER BY d.published_date DESC NULLS LAST, c.fact_key, e.claim_id LIMIT 1
        ) AS claim_id
        FROM corpus_gap g
        WHERE g.issuer_id = %s AND g.resolved_at IS NULL AND g.scope = %s
        ORDER BY g.agency, g.instrument_class, g.term, g.action_date
        """,
        (issuer_id, scope),
    ).fetchall()
