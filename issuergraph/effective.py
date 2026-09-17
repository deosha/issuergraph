"""Effective-dated rating states: what each agency says is true, and when.

A rating action is not an event confined to the day it was published — it is the
start of a period during which that view stands. ICRA's Negative outlook of 24
September 2025 is still ICRA's position in January 2026 because ICRA has not
said otherwise, so a rating published by another agency in January is concurrent
with it and comparable to it.

Bucketing by calendar quarter approximated this badly in both directions: it
merged two of an agency's own successive actions that happened to land in the
same three months, and it separated genuinely concurrent views that straddled a
boundary — 30 June and 2 July are two days apart and two different buckets.

So each (agency, instrument class, rating scale) gets a timeline. A state runs
from its action date until the same agency's next action on the same instrument
class, or forever if there is none yet. Overlap between two agencies' states is
then a plain interval intersection, and it is the only thing that licenses
calling their difference a disagreement.

The table is derived: it is rebuilt from rating_action on every run and holds no
history of its own. Conflict rows hold the history.
"""
from __future__ import annotations

from datetime import date


def is_withdrawal(action: str | None) -> bool:
    """Does this action end the rating on the tranche it names?

    Agencies write "reaffirmed and withdrawn": the rating was affirmed as the
    last word on that tranche and then withdrawn, typically because the paper
    was repaid. The tranche has no rating after the action date.
    """
    return bool(action) and "withdrawn" in action.casefold()


def _states(rows: list[dict]) -> list[dict]:
    """Close each state where the same agency next acts on the same class.

    A withdrawal closes its own state on the action date instead. It applies to
    the tranche the row names, not to the whole class: an agency withdrawing
    one repaid NCD tranche while reaffirming the others leaves the class rated,
    so the other rows of the same action keep their open-ended state.
    """
    timelines: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (row["agency"], row["instrument_class"], row["term"])
        timelines.setdefault(key, []).append(row)

    states: list[dict] = []
    for timeline in timelines.values():
        timeline.sort(key=lambda r: (r["action_date"], r["claim_id"]))
        # One action can rate several tranches of the same class identically;
        # those are one state, not several, so successive dates are the unit.
        dates = sorted({r["action_date"] for r in timeline})
        next_date = {d: dates[i + 1] if i + 1 < len(dates) else None
                     for i, d in enumerate(dates)}
        for row in timeline:
            withdrawn = is_withdrawal(row.get("action"))
            states.append({**row,
                           "effective_from": row["action_date"],
                           "effective_to": (row["action_date"] if withdrawn
                                            else next_date[row["action_date"]]),
                           "withdrawn": withdrawn})
    return states


def overlaps(a: dict, b: dict) -> tuple[date, date | None] | None:
    """The interval during which both states were simultaneously in force.

    Half-open [from, to): a state that ends on the day another begins is the
    handover between two of one agency's own actions, not a moment when both
    were true.
    """
    start = max(a["effective_from"], b["effective_from"])
    ends = [x["effective_to"] for x in (a, b) if x["effective_to"] is not None]
    end = min(ends) if ends else None
    if end is not None and end <= start:
        return None
    return start, end


def build_rating_states(conn, issuer_id: int) -> int:
    """Rebuild the issuer's rating timelines from its rating actions."""
    rows = conn.execute(
        """
        SELECT r.claim_id, r.agency, r.instrument, r.instrument_class, r.term,
               r.rating AS grade, r.outlook, r.watch, r.action, r.action_date
        FROM rating_action r
        JOIN claim c ON c.id = r.claim_id
        WHERE c.issuer_id = %s AND r.action_date IS NOT NULL
        ORDER BY r.action_date, r.agency, r.claim_id
        """,
        (issuer_id,),
    ).fetchall()

    conn.execute("DELETE FROM rating_state WHERE issuer_id = %s", (issuer_id,))
    states = _states([dict(r) for r in rows])
    for state in states:
        conn.execute(
            """
            INSERT INTO rating_state (issuer_id, claim_id, agency, instrument_class,
                                      term, instrument, grade, outlook, watch,
                                      effective_from, effective_to, withdrawn)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (issuer_id, state["claim_id"], state["agency"], state["instrument_class"],
             state["term"], state["instrument"], state["grade"], state["outlook"],
             state["watch"], state["effective_from"], state["effective_to"],
             state["withdrawn"]),
        )
    return len(states)


def concurrent_pairs(conn, issuer_id: int):
    """Every pair of states from different agencies that were in force together.

    Same instrument class and same rating scale, because a long-term AA and a
    short-term A1+ are not two opinions about one quantity.
    """
    rows = conn.execute(
        """
        SELECT * FROM rating_state WHERE issuer_id = %s
        ORDER BY instrument_class, term, effective_from, agency
        """,
        (issuer_id,),
    ).fetchall()

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["instrument_class"], row["term"]), []).append(dict(row))

    for (instrument_class, term), states in sorted(groups.items()):
        for i, a in enumerate(states):
            for b in states[i + 1:]:
                if a["agency"] == b["agency"]:
                    continue        # one agency revising itself is a change, not a conflict
                window = overlaps(a, b)
                if window:
                    yield instrument_class, term, a, b, window
