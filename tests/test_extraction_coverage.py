"""Silent omission and assumed units — the two ways a perfect anchor lies.

Both failures are invisible in the happy-path corpus, so both are tested by
mutating the real page text: the pages are read from the database and edited in
memory, which exercises the extractor against text that differs from the corpus
exactly the way a reprinted report would.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from issuergraph.db import one, query
from issuergraph.extractors import annual_report as ar
from issuergraph.extractors.coverage import Coverage, check, default_coverage
from issuergraph.extractors.units import CANONICAL, UnknownUnit, find_unit


@pytest.fixture(scope="module")
def pages():
    rows = query(
        """
        SELECT page_no, text FROM document_page p
        JOIN document d ON d.id = p.document_id
        WHERE d.doc_type = 'annual_report'
        ORDER BY page_no
        """
    )
    if not rows:
        pytest.skip("annual report not ingested — run the pipeline first")
    return rows


def mutate(pages, old: str, new: str):
    return [{"page_no": p["page_no"], "text": p["text"].replace(old, new)} for p in pages]


def totals(claims):
    return {c.fact_key: c for c in claims if c.claim_type == "total_borrowings"}


# --- units are read, not assumed --------------------------------------------

def test_the_corpus_declares_crores(pages):
    unit = find_unit(next(p["text"] for p in pages if "BALANCE SHEET" in p["text"]))
    assert unit is not None and unit.scale == "crore" and unit.is_canonical


def test_lakhs_are_converted_not_relabelled(pages):
    """The reviewer's mutation: same digits, different declared scale.

    Anchoring cannot catch this — every digit is still exactly where the anchor
    says. Only reading the unit can.
    """
    crores = totals(ar.extract(None, pages))
    lakhs = totals(ar.extract(None, mutate(pages, "` in Crores)", "` in Lakhs)")))
    assert set(crores) == set(lakhs)
    for key, claim in lakhs.items():
        assert claim.value_unit == CANONICAL
        assert Decimal(claim.value_numeric) == (
            Decimal(crores[key].value_numeric) / 100).quantize(Decimal("0.01")), key


def test_millions_are_converted(pages):
    """Conversion happens per component, and the total stays their exact sum.

    Converting each row and summing can differ in the last paisa from converting
    the crore total, and of the two the per-row order is the one that keeps the
    total equal to the components its own anchors show. A total that does not
    equal its evidence is the defect worth avoiding here.
    """
    claims = ar.extract(None, mutate(pages, "` in Crores)", "(₹ in Million)"))
    assert claims, "the unit pattern must tolerate a real rupee sign"

    crores = totals(ar.extract(None, pages))
    for key, claim in totals(claims).items():
        components = sum(
            Decimal(c.value_numeric) for c in claims
            if c.claim_type == "debt_instrument" and c.basis == claim.basis
            and c.as_of_date == claim.as_of_date
        )
        assert Decimal(claim.value_numeric) == components, key
        # and it is the right order of magnitude: millions are a tenth of a crore
        assert abs(Decimal(claim.value_numeric)
                   - Decimal(crores[key].value_numeric) / 10) <= Decimal("0.02")


def test_a_converted_claim_says_so_in_its_subject(pages):
    claim = next(iter(totals(ar.extract(
        None, mutate(pages, "` in Crores)", "` in Lakhs)"))).values()))
    assert "converted from lakh" in claim.subject


def test_undeclared_unit_yields_no_claims_and_a_reason(pages):
    stripped = mutate(pages, "(` in Crores)", "")
    claims = ar.extract(None, stripped)
    assert claims == []
    cov = check(ar, None, stripped, claims)
    assert not cov.complete
    assert any("no monetary unit declared" in m for m in cov.missing)


def test_unknown_scale_raises_rather_than_guessing():
    """A scale we cannot convert must not be mistaken for no scale at all."""
    with pytest.raises(UnknownUnit):
        find_unit("BALANCE SHEET (` in trillions)")


def test_an_unreadable_scale_is_reported_distinctly(pages):
    odd = mutate(pages, "` in Crores)", "` in trillions)")
    claims = ar.extract(None, odd)
    assert claims == []
    missing = check(ar, None, odd, claims).missing
    assert any("unrecognised scale" in m for m in missing), missing


def test_no_declaration_is_not_a_unit():
    assert find_unit("BALANCE SHEET\nDebt securities\n1,234.00") is None


def test_the_nearest_declaration_above_the_table_wins():
    text = "(` in Lakhs)\nsome other section\n(` in Crores)\nBALANCE SHEET\n"
    assert find_unit(text, before=len(text)).scale == "crore"


def test_every_stored_amount_carries_a_unit():
    """No numeric claim in the database may be unitless."""
    bad = query(
        "SELECT id, subject FROM claim WHERE value_numeric IS NOT NULL AND value_unit IS NULL"
    )
    assert bad == [], f"numeric claims without a unit: {bad}"


def test_balance_sheet_claims_anchor_their_unit():
    """The citation must prove the magnitude, not only the digits."""
    rows = query(
        """
        SELECT c.id, c.subject,
               count(*) FILTER (WHERE a.evidence_text ILIKE '%%crore%%') AS unit_anchors
        FROM claim c JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE c.extractor = 'annual_report_balance_sheet'
        GROUP BY c.id, c.subject
        """
    )
    assert rows, "no balance-sheet claims found"
    assert all(r["unit_anchors"] >= 1 for r in rows), \
        [r["subject"] for r in rows if not r["unit_anchors"]]


# --- omissions are loud -----------------------------------------------------

def test_a_reworded_row_is_reported_not_swallowed(pages):
    """The failure that matters: fewer facts, no error, every anchor still valid."""
    moved = mutate(pages, "Subordinated liabilities", "Subordinated liabilities, net")
    claims = ar.extract(None, moved)
    cov = check(ar, None, moved, claims)
    assert claims == []
    assert not cov.complete
    assert any("Subordinated liabilities" in m for m in cov.missing)
    assert any("consolidated" in m for m in cov.missing)
    assert any("standalone" in m for m in cov.missing)


def test_a_missing_statement_is_reported(pages):
    gone = mutate(pages, "CONSOLIDATED BALANCE SHEET", "SUMMARY OF FINANCIAL POSITION")
    claims = ar.extract(None, gone)
    cov = check(ar, None, gone, claims)
    assert not cov.complete
    assert any("no consolidated balance sheet found" in m for m in cov.missing)
    # the standalone statement still parses: a partial failure stays partial
    assert {c.basis for c in claims} == {"standalone"}


def test_case_changes_in_the_heading_do_not_break_parsing(pages):
    """The locator is looser than the parser, so cosmetics are survivable."""
    retitled = mutate(pages, "CONSOLIDATED BALANCE SHEET", "Consolidated Balance Sheet")
    claims = ar.extract(None, retitled)
    assert check(ar, None, retitled, claims).complete
    assert len(claims) == len(ar.extract(None, pages))


def test_a_partial_statement_emits_nothing_for_that_basis(pages):
    """Two of three components would sum to a wrong number that looks right."""
    broken = mutate(pages, "Debt securities", "Debt instruments")
    claims = ar.extract(None, broken)
    assert not any(c.claim_type == "total_borrowings" for c in claims)


def test_coverage_is_recorded_for_every_document():
    rows = query("SELECT id, source_name, extraction_status FROM document")
    assert rows, "no documents ingested"
    assert all(r["extraction_status"] in ("complete", "incomplete") for r in rows), rows


def test_the_corpus_extracts_completely():
    incomplete = query(
        "SELECT source_name, extraction_missing FROM document "
        "WHERE extraction_status <> 'complete'"
    )
    assert incomplete == [], f"documents not fully extracted: {incomplete}"


def test_annual_report_expects_sixteen_facts(pages):
    """The declaration itself, so a future edit cannot quietly lower the bar."""
    claims = ar.extract(None, pages)
    assert len(claims) == 16
    assert len(totals(claims)) == 4      # two bases x two comparative columns


def test_incomplete_documents_must_carry_reasons():
    """The database refuses a status with no explanation (sql/003)."""
    bad = one(
        "SELECT id FROM document WHERE extraction_status = 'incomplete' "
        "AND cardinality(extraction_missing) = 0"
    )
    assert bad is None


# --- the coverage primitive -------------------------------------------------

def test_default_coverage_is_honest_about_its_weakness():
    assert default_coverage([]).complete is False
    assert default_coverage(["anything"]).complete is True


def test_coverage_merges():
    a, b = Coverage(), Coverage()
    a.require(True, "fine")
    b.require(False, "broken")
    assert a.merge(b).missing == ["broken"]
    assert a.expected == 2 and a.found == 1
