"""CARE's 2024 actions on IIFL Finance, read as the releases state them.

The demo showed CARE rating IIFL's long-term debentures AA, in force from
12 March 2024 onward. Two later CARE releases say otherwise:

  * 13 Apr 2024 — watch direction revised from Developing to Negative.
  * 19 Sep 2024 — bank facilities and long-term instruments downgraded to
    AA-/Stable; the Non-Convertible Debentures rating *withdrawn*, with no
    grade stated.

So on 15 Sep 2025 CARE had no debenture rating at all — neither the AA the
demo showed nor an AA- the downgrade might suggest. The extractor fixes these
tests lock down: table rows are delimited by the label column rather than
guessed from text, the rating cell is read apart from the action wording (which
names the watch it moved *from*), a gradeless withdrawal is a claim rather
than a dropped row, coverage requires every table row to become one, and the
generic "Long Term Instruments" row takes its class from the Annexure-1 line
that names it.
"""
from __future__ import annotations

from datetime import date

import pytest

from issuergraph.db import one, query
from issuergraph.extractors import care
from issuergraph.models import DocumentMeta, RatingDetail

ON = date(2025, 9, 15)
SEPTEMBER_URL = ("https://www.careratings.com/upload/CompanyFiles/PR/"
                 "202409170957_IIFL_Finance_Limited.pdf")


def in_force(instrument_class: str, on: date) -> list[dict]:
    return query(
        """
        SELECT grade, outlook, watch, withdrawn FROM rating_state
        WHERE agency = 'CARE' AND instrument_class = %s AND term = 'long_term'
          AND effective_from <= %s AND (effective_to IS NULL OR effective_to > %s)
        """,
        (instrument_class, on, on),
    )


@pytest.fixture(scope="module")
def september():
    doc = one("SELECT id FROM document WHERE url = %s", (SEPTEMBER_URL,))
    if not doc:
        pytest.skip("corpus not loaded")
    pages = query("SELECT page_no, text, word_map FROM document_page "
                  "WHERE document_id = %s ORDER BY page_no", (doc["id"],))
    meta = DocumentMeta(doc_type="rating_rationale", source_name="CARE",
                        title="t", url=SEPTEMBER_URL)
    return doc["id"], meta, pages


# --- the regression -----------------------------------------------------------

def test_care_has_no_debenture_rating_in_force_on_15_september_2025():
    """Not AA (the March view) and not AA- (the downgrade): withdrawn."""
    assert in_force("ncd", ON) == []


def test_the_march_debenture_aa_ends_where_care_acted_next():
    states = query(
        """
        SELECT grade, watch, effective_from, effective_to, withdrawn FROM rating_state
        WHERE agency = 'CARE' AND instrument_class = 'ncd' ORDER BY effective_from
        """)
    assert [(s["grade"], s["effective_from"], s["effective_to"], s["withdrawn"])
            for s in states] == [
        ("AA", date(2024, 3, 12), date(2024, 4, 13), False),
        ("AA", date(2024, 4, 13), date(2024, 9, 19), False),
        (None, date(2024, 9, 19), date(2024, 9, 19), True),
    ]
    assert [s["watch"] for s in states[:2]] == [
        "Watch with Developing Implications", "Watch with Negative Implications"]


def test_care_rates_nothing_on_15_september_2025():
    """CARE withdrew its bank-facility and subordinated-debt ratings on
    20 Sep 2024, the day after the downgrade; its AA-/Stable stood one day."""
    for cls in ("bank_facility", "subordinated_debt", "ncd"):
        assert in_force(cls, ON) == [], cls
    assert in_force("bank_facility", date(2024, 9, 19)) == [
        {"grade": "AA-", "outlook": "Stable", "watch": None, "withdrawn": False}]


def test_the_withdrawal_is_anchored_to_its_row():
    row = one(
        """
        SELECT r.rating, r.action, c.value_text,
               array_agg(a.evidence_text ORDER BY a.id) AS evidence
        FROM rating_action r JOIN claim c ON c.id = r.claim_id
        JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE r.agency = 'CARE' AND r.instrument_class = 'ncd'
          AND r.action_date = '2024-09-19'
        GROUP BY r.rating, r.action, c.value_text
        """)
    assert row["rating"] is None and row["action"] == "withdrawn"
    assert row["evidence"] == ["Non-Convertible\nDebentures", "-", "-", "Withdrawn"]


# --- the extractor ------------------------------------------------------------

def test_rows_are_not_glued_to_the_previous_rows_action(september):
    _, meta, pages = september
    ratings = [c.rating for c in care.extract(meta, pages) if c.rating]
    assert [(r.instrument, r.rating, r.outlook, r.previous_rating) for r in ratings] == [
        ("Long Term Bank Facilities", "AA-", "Stable", "AA"),
        ("Long Term Instruments", "AA-", "Stable", "AA"),
        ("Non-Convertible Debentures", None, None, None),
    ]


def test_the_watch_is_read_from_the_rating_cell_not_the_action_wording():
    """April's action says "from Developing to Negative"; the cell says (RWN)."""
    watches = {r["watch"] for r in query(
        "SELECT watch FROM rating_action WHERE agency = 'CARE' AND action_date = '2024-04-13'")}
    assert watches == {"Watch with Negative Implications"}


def test_coverage_names_a_table_row_that_became_nothing(september):
    _, meta, pages = september
    claims = [c for c in care.extract(meta, pages)
              if not (c.rating and c.rating.rating is None)]
    cov = care.check_coverage(meta, pages, claims)
    assert not cov.complete
    assert cov.missing == [
        "CARE: table row 'Non-Convertible Debentures' on page 1 "
        "yielded no rating or withdrawal"]


def test_the_extractor_refuses_pages_without_word_boxes(september):
    _, meta, pages = september
    with pytest.raises(ValueError, match="word_map"):
        care.extract(meta, [{"page_no": p["page_no"], "text": p["text"]} for p in pages])


def test_only_a_withdrawal_may_omit_its_grade():
    RatingDetail(agency="CARE", instrument="NCD", rating=None, action="withdrawn")
    with pytest.raises(ValueError, match="withdrawal"):
        RatingDetail(agency="CARE", instrument="NCD", rating=None, action="downgraded")


def test_long_term_instruments_are_the_subordinated_debt_the_annexure_names():
    """Classified from Annexure-1, with that line as evidence on the claim."""
    rows = query(
        """
        SELECT r.action_date, r.instrument_class,
               array_agg(a.evidence_text ORDER BY a.id) AS evidence
        FROM rating_action r JOIN claim c ON c.id = r.claim_id
        JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE r.agency = 'CARE' AND r.instrument ILIKE '%%Instruments'
        GROUP BY r.action_date, r.instrument_class ORDER BY r.action_date
        """)
    assert [(r["action_date"], r["instrument_class"]) for r in rows] == [
        (date(2024, 3, 12), "subordinated_debt"),
        (date(2024, 4, 13), "subordinated_debt"),
        (date(2024, 9, 19), "subordinated_debt"),
        (date(2024, 9, 20), "subordinated_debt"),     # the withdrawal: no amount,
    ]                                                 # the one unclaimed 0.00 line
    assert [r["evidence"][-2:] for r in rows] == [
        ["Subordinated debt", "100.00"], ["Subordinated debt", "100.00"],
        ["Subordinate\nDebt", "100.00"], ["Subordinate\nDebt", "0.00"]]


def test_care_and_icra_disagreed_on_subordinated_debt_for_one_day():
    row = one("SELECT note, ended_on, resolved_at FROM conflict WHERE fact_key = "
              "'rating_grade|subordinated_debt|long_term|CARE~ICRA|2024-09-19'")
    assert row and row["ended_on"] == date(2024, 9, 20) and row["resolved_at"] is None
    assert "CARE says AA-" in row["note"] and "ICRA says AA" in row["note"]


def test_an_ambiguous_annexure_size_classifies_nothing():
    """Two named instruments of one size: no class is borrowed."""
    annexure = [{"name": "Subordinated debt", "size": 100},
                {"name": "Non-convertible debenture", "size": 100}]
    assert care._named_in_annexure(annexure, 100) is None
    assert care._named_in_annexure(annexure[:1], 100)["name"] == "Subordinated debt"


# --- history ------------------------------------------------------------------

def test_the_resolved_brickwork_care_conflict_keeps_both_sides():
    """Re-extracting the March release replaced its claims; the resolved
    conflict must still name what CARE said, from CARE's own document."""
    row = one("SELECT id, resolved_at FROM conflict "
              "WHERE fact_key = 'rating_grade|ncd|long_term|Brickwork~CARE|2025-09-15'")
    assert row and row["resolved_at"] is not None
    members = query(
        """
        SELECT d.source_name, m.stated_value FROM conflict_member m
        JOIN claim c ON c.id = m.claim_id JOIN document d ON d.id = c.document_id
        WHERE m.conflict_id = %s ORDER BY d.source_name, m.stated_value
        """,
        (row["id"],))
    assert {(m["source_name"], m["stated_value"]) for m in members} == {
        ("Brickwork", "AA+"), ("CARE", "AA")}
