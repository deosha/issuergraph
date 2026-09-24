"""Brickwork's Nov/Dec 2025 layouts, and Crisil's 2024 and 2026 variants.

Read from the stored documents (skipped when not loaded). Each test names the
failure it locks down.
"""
from __future__ import annotations

import collections
from datetime import date
from decimal import Decimal

import pytest

from issuergraph.db import query
from issuergraph.extractors import brickwork, crisil
from issuergraph.models import DocumentMeta


def pages(url_part: str) -> list[dict]:
    rows = query("SELECT p.page_no, p.text, p.word_map FROM document_page p "
                 "JOIN document d ON d.id = p.document_id WHERE d.url LIKE %s "
                 "ORDER BY p.page_no", (f"%{url_part}%",))
    if not rows:
        pytest.skip(f"{url_part} not loaded")
    return [dict(r) for r in rows]


META = DocumentMeta(doc_type="rating_rationale", source_name="Brickwork", title="t", url="u")


def ratings(url_part):
    return [c.rating for c in brickwork.extract(META, pages(url_part)) if c.rating]


def test_merged_cells_are_split_by_position():
    """Dec 2025 prints "NCDs Public Issue ** 975.82" and "2000.00 Long Term" as one
    line each; read by word position they are separate cells."""
    rows = sorted((r.instrument, r.rated_amount_cr, r.rating, r.action)
                  for r in ratings("IIFL-Finance-24Dec2025"))
    assert rows == sorted([
        ("NCDs Public Issue", Decimal("975.82"), "AA+", "reaffirmed"),
        ("NCDs", Decimal("46.22"), "AA+", "reaffirmed"),
        ("NCDs (Proposed - Public issue)", Decimal("2000.00"), "AA+", "reaffirmed"),
        ("Perpetual Debt Instrument (PDI)", Decimal("500.00"), "AA", "reaffirmed"),
        ("Proposed Perpetual Debt Instrument (PDI)", Decimal("150.00"), "AA", "assigned"),
    ])


def test_an_assignment_is_an_action_and_the_total_line_is_not_a_rating():
    nov = {r.instrument: r for r in ratings("IIFL-Finance-21Nov2025")}
    assert nov["NCDs (Proposed - public issue)"].action == "assigned"
    sep = [c for c in brickwork.extract(META, pages("IIFL-Finance-15Sep2025")) if c.rating]
    assert all("Total" not in c.value_text for c in sep)


def test_a_tall_previous_rating_stays_with_its_row():
    sep = {r.instrument: r.previous_rating for r in ratings("IIFL-Finance-15Sep2025")}
    assert sep["NCDs Public Issue"] == ("BWR AA+/Stable (Reaffirmed and removed Rating Watch "
                                        "with Negative Implications)")
    assert sep["NCDs"] is None


def history(url_part):
    return [c.history for c in brickwork.extract(META, pages(url_part)) if c.history]


def test_history_rows_follow_reading_order_not_position():
    """Cells taller than their rows: a perpetual instrument assigned in Sep 2025
    has no 2022 history, and NCDs withdrawn in 2023 have no 2025 history."""
    h = history("IIFL-Finance-24Dec2025")
    by = collections.defaultdict(set)
    for e in h:
        by[e.instrument_class if e.instrument_class == "perpetual_debt" else e.instrument].add(
            e.action_date.year)
    assert by["perpetual_debt"] == {2025}
    assert by["Secured NCDs"] == {2022}


def test_an_entry_above_its_rows_amount_is_not_dropped():
    """Row 1's 30 Sep 2024 entry starts higher on the page than row 1's amount."""
    dated = collections.Counter(e.action_date for e in history("IIFL-Finance-21Nov2025")
                                if e.instrument == "NCDs")
    assert dated[date(2024, 9, 30)] == 2 and dated[date(2024, 3, 13)] == 2


def test_the_new_layout_meets_its_coverage():
    from issuergraph.extractors import coverage
    for part in ("IIFL-Finance-21Nov2025", "IIFL-Finance-24Dec2025"):
        p = pages(part)
        cov = coverage.check(brickwork, META, p, brickwork.extract(META, p))
        assert cov.complete, cov.missing


def crisil_units(url_part):
    from issuergraph import htmldoc
    row = query("SELECT d.content_type, b.bytes FROM document d JOIN document_blob b "
                "ON b.document_id = d.id WHERE d.url LIKE %s", (f"%{url_part}%",))
    if not row:
        pytest.skip(f"{url_part} not loaded")
    return htmldoc.units(htmldoc.parse(bytes(row[0]["bytes"]), row[0]["content_type"]))


def test_crisil_2024_headings_carry_a_colon():
    claims = crisil.extract(META, crisil_units("September%2030_%202024"))
    found = {c.fact_key: c.value_text for c in claims
             if c.fact_key in ("outlook_statement|CRISIL", "liquidity_assessment|CRISIL")}
    assert found == {"outlook_statement|CRISIL": "Stable", "liquidity_assessment|CRISIL": "Strong"}


def test_crisil_2026_percent_marker_means_secured():
    claims = crisil.extract(META, crisil_units("March%2024_%202026"))
    marks = [c.rating.qualifiers for c in claims
             if c.rating and c.rating.rated_amount_cr is not None and "secured" in c.rating.qualifiers]
    assert marks == [["retail", "secured"]]
