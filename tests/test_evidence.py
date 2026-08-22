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


def test_every_anchor_has_geometry(conn):
    missing = query("SELECT id FROM evidence_anchor WHERE bbox IS NULL OR bbox_rects IS NULL")
    assert missing == [], f"{len(missing)} anchors cannot be highlighted"


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
    assert len(anchors) == 3
    components = sum(Decimal(a["evidence_text"].replace(",", "")) for a in anchors)
    assert components == total["value_numeric"]


# --- disagreement is preserved, never resolved ------------------------------

def test_agencies_disagree_on_the_outlook_and_we_say_so(conn):
    conflict = one(
        "SELECT id, note FROM conflict WHERE fact_key = 'rating_outlook|long_term|2025Q3'"
    )
    assert conflict, "Sept 2025: ICRA said Negative, Brickwork said Stable"
    members = query(
        """
        SELECT c.value_text, d.source_name FROM conflict_member m
        JOIN claim c ON c.id = m.claim_id JOIN document d ON d.id = c.document_id
        WHERE m.conflict_id = %s
        """,
        (conflict["id"],),
    )
    assert {(m["source_name"], m["value_text"]) for m in members} == {
        ("ICRA", "Negative"), ("Brickwork", "Stable")}


def test_watch_direction_disagreement_on_the_same_day(conn):
    conflict = one(
        "SELECT id FROM conflict WHERE fact_key = 'rating_watch|long_term|2024Q1'"
    )
    assert conflict, "12 March 2024: ICRA said Negative watch, CARE said Developing"


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
    rows = query(
        """
        SELECT direction, from_text, to_text FROM rationale_diff
        WHERE agency = 'ICRA' AND section = 'rating' AND to_date = '2025-09-24'
        """
    )
    texts = {(r["direction"], r["from_text"] or r["to_text"]) for r in rows}
    assert ("removed", "Watch with Negative Implications") in texts
    assert ("added", "Negative") in texts
