"""The acceptance test, executable.

Run the pipeline first, then: .venv/bin/pytest -q
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from issuergraph.db import connect, one, query


@pytest.fixture(scope="module")
def conn():
    with connect() as c:
        yield c


# --- the invariant: no source = no fact -------------------------------------

def test_every_claim_has_evidence(conn):
    orphans = query(
        """
        SELECT c.id, c.subject FROM claim c
        LEFT JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE a.id IS NULL
        """
    )
    assert orphans == [], f"claims without evidence: {orphans}"


def test_anchor_text_matches_the_page_exactly(conn):
    mismatches = query(
        """
        SELECT a.id, a.page_no, a.evidence_text,
               substring(p.text FROM a.char_start + 1 FOR a.char_end - a.char_start) AS actual
        FROM evidence_anchor a
        JOIN document_page p ON p.document_id = a.document_id AND p.page_no = a.page_no
        WHERE substring(p.text FROM a.char_start + 1 FOR a.char_end - a.char_start)
              IS DISTINCT FROM a.evidence_text
        """
    )
    assert mismatches == [], f"{len(mismatches)} anchors drifted from their page text"


def test_every_anchor_can_be_shown(conn):
    """A PDF anchor has rectangles to highlight; an HTML anchor has a node
    path instead, and no geometry (its evidence is shown as a quote and a link)."""
    missing = query("SELECT id FROM evidence_anchor WHERE kind = 'pdf' "
                    "AND (bbox IS NULL OR bbox_rects IS NULL)")
    assert missing == [], f"{len(missing)} PDF anchors cannot be highlighted"
    unlocated = query("SELECT id FROM evidence_anchor WHERE kind = 'html' "
                      "AND (node_path IS NULL OR bbox IS NOT NULL)")
    assert unlocated == [], f"{len(unlocated)} HTML anchors are not located by node path"


# --- the numbers ------------------------------------------------------------

@pytest.mark.parametrize("fact_key,expected", [
    ("total_borrowings|consolidated|2025-03-31", Decimal("51068.03")),
    ("total_borrowings|consolidated|2024-03-31", Decimal("46674.20")),
    ("total_borrowings|standalone|2025-03-31", Decimal("24524.16")),
    ("total_borrowings|standalone|2024-03-31", Decimal("19985.90")),
])
def test_annual_report_totals(conn, fact_key, expected):
    row = one(
        "SELECT value_numeric FROM claim "
        "WHERE fact_key = %s AND extractor = 'annual_report_balance_sheet'",
        (fact_key,),
    )
    assert row and row["value_numeric"] == expected


def test_total_is_the_sum_of_its_three_anchored_components(conn):
    total = one(
        "SELECT id, value_numeric FROM claim WHERE fact_key = %s AND extractor = %s",
        ("total_borrowings|consolidated|2025-03-31", "annual_report_balance_sheet"),
    )
    anchors = query(
        "SELECT evidence_text FROM evidence_anchor WHERE claim_id = %s ORDER BY ordinal",
        (total["id"],),
    )
    # Four anchors, not three: the three component amounts that make up the sum,
    # plus the statement's own unit declaration. The arithmetic is proved by the
    # first three; the magnitude is proved by the fourth, and a number without
    # its unit is not a verified fact.
    amounts = [a["evidence_text"] for a in anchors
               if a["evidence_text"].replace(",", "").replace(".", "").isdigit()]
    units = [a["evidence_text"] for a in anchors if a["evidence_text"] not in amounts]
    assert len(amounts) == 3, anchors
    assert len(units) == 1 and "crore" in units[0].lower(), units
    assert sum(Decimal(a.replace(",", "")) for a in amounts) == total["value_numeric"]


# --- disagreement is preserved, never resolved ------------------------------

def test_agencies_disagree_on_the_outlook_and_we_say_so(conn):
    """The same real disagreement the quarter key used to express, now dated.

    The key is (aspect, instrument class, scale, agency pair, start of the run)
    rather than a calendar quarter: Brickwork and ICRA differ on the outlook for
    non-convertible debentures, and have done since ICRA's action of 24
    September 2025.
    """
    conflict = one(
        "SELECT id, note, subject FROM conflict "
        "WHERE fact_key = 'rating_outlook|ncd|long_term|Brickwork~ICRA|2025-09-24'"
    )
    assert conflict, "Sept 2025: ICRA said Negative, Brickwork said Stable"
    assert "non-convertible debentures" in conflict["subject"]
    members = query(
        """
        SELECT m.stated_value, c.value_text, d.source_name FROM conflict_member m
        JOIN claim c ON c.id = m.claim_id JOIN document d ON d.id = c.document_id
        WHERE m.conflict_id = %s
        """,
        (conflict["id"],),
    )
    # stated_value is what each side said about *the outlook*; the claim keeps
    # the whole rating line it was read from, which is the evidence.
    assert {(m["source_name"], m["stated_value"]) for m in members} == {
        ("ICRA", "Negative"), ("Brickwork", "Stable")}
    assert all(m["stated_value"] in m["value_text"] for m in members)


@pytest.mark.parametrize("instrument_class", ["ncd", "bank_facility"])
def test_watch_direction_disagreement_on_the_same_day(conn, instrument_class):
    """12 March 2024: ICRA said Negative watch, CARE said Developing.

    Once per instrument class, because that is what each agency actually rated.
    The old key compared CARE's bank facilities against ICRA's debenture
    programme and called it one conflict.
    """
    conflict = one(
        "SELECT id, subject FROM conflict WHERE fact_key = %s",
        (f"rating_watch|{instrument_class}|long_term|CARE~ICRA|2024-03-12",),
    )
    assert conflict, f"no {instrument_class} watch conflict on 12 March 2024"


def test_numeric_conflict_is_above_tolerance_and_names_both_sides(conn):
    row = one(
        "SELECT spread_pct, tolerance_pct, note FROM conflict "
        "WHERE fact_key = 'total_borrowings|standalone|2024-03-31'"
    )
    assert row and row["spread_pct"] > row["tolerance_pct"]
    assert "Brickwork" in row["note"] and "Company" in row["note"]


def test_no_conflict_is_silently_resolved(conn):
    lonely = query(
        """
        SELECT k.id FROM conflict k
        LEFT JOIN conflict_member m ON m.conflict_id = k.id
        GROUP BY k.id HAVING count(m.claim_id) < 2
        """
    )
    assert lonely == [], "a conflict must link at least two claims"


# --- what changed -----------------------------------------------------------

def test_successive_icra_reports_are_diffed(conn):
    row = one(
        """
        SELECT from_text, to_text FROM rationale_diff
        WHERE agency = 'ICRA' AND section = 'liquidity' AND direction = 'changed'
          AND from_date = '2025-09-24' AND to_date = '2026-02-11'
        """
    )
    assert row and "July 31, 2025" in row["from_text"] and "December 31, 2025" in row["to_text"]


def test_watch_to_outlook_transition_is_visible(conn):
    """ICRA dropped its watch for a Stable outlook on 25 Sep 2024, then moved
    the outlook to Negative on 24 Sep 2025. Each change shows up between the
    two reports that actually contain it."""
    def diffs(to_date):
        return {(r["direction"], r["from_text"], r["to_text"]) for r in query(
            """
            SELECT direction, from_text, to_text FROM rationale_diff
            WHERE agency = 'ICRA' AND section = 'rating' AND to_date = %s
            """, (to_date,))}
    september_2024 = diffs("2024-09-25")
    assert ("removed", "Watch with Negative Implications", None) in september_2024
    assert ("added", None, "Stable") in september_2024
    assert diffs("2025-09-24") == {("changed", "Stable", "Negative")}
