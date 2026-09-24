"""Crisil Ratings, the fourth rating publisher, read from its HTML rationale.

Built from the stored reference document — IIFL Finance, 27 January 2026 —
read out of document_blob at test time. The page's bytes are never committed:
Crisil's terms restrict redistribution, and a fixture in git would be a copy.
Tests skip when the document is not loaded.
"""
from __future__ import annotations

import collections
import re
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from issuergraph import api, evidence, htmldoc
from issuergraph.db import connect, one, query
from issuergraph.extractors import coverage, crisil
from issuergraph.models import DocumentMeta
from issuergraph.reconcile import ADJUSTED_VS_REPORTED, _comparable_keys

import asgi

URL_PART = "IIFLFinanceLimited_January%2027_%202026_RR_387837"


@pytest.fixture(scope="module")
def stored():
    row = one(
        """
        SELECT d.id, d.content_type, b.bytes FROM document d
        JOIN document_blob b ON b.document_id = d.id WHERE d.url LIKE %s
        """, (f"%{URL_PART}%",))
    if not row:
        pytest.skip("Crisil reference rationale not loaded")
    units = htmldoc.units(htmldoc.parse(bytes(row["bytes"]), row["content_type"]))
    meta = DocumentMeta(doc_type="rating_rationale", source_name="CRISIL", title="t", url="u")
    out = crisil.read(meta, units)
    return {"id": row["id"], "units": units, "meta": meta, **out}


def ratings(stored):
    return [c.rating for c in stored["claims"] if c.rating]


# --- normalisation ------------------------------------------------------------

@pytest.mark.parametrize("raw, grade, outlook, watch, qualifiers", [
    ("Crisil AA/Stable (Reaffirmed)", "AA", "Stable", None, []),
    ("Crisil AA-/Stable (Reaffirmed)", "AA-", "Stable", None, []),
    ("Crisil A1+ (Reaffirmed)", "A1+", None, None, []),
    ("Crisil PPMLD AA/Stable", "AA", "Stable", None, ["ppmld"]),
    ("Crisil PPMLD AA r /Stable", "AA", "Stable", None, ["ppmld", "legacy_r"]),
    ("Crisil AA/Watch Developing", "AA", None, "Watch with Developing Implications", []),
    ("Crisil AA-/Positive", "AA-", "Positive", None, []),
])
def test_crisil_grades_normalise_to_the_shared_vocabulary(raw, grade, outlook, watch, qualifiers):
    r = crisil.read_rating(raw)
    assert (r["grade"], r["outlook"], r["watch"], r["qualifiers"]) == (
        grade, outlook, watch, qualifiers)


def test_withdrawn_is_a_gradeless_withdrawal():
    r = crisil.read_rating("Withdrawn")
    assert (r["grade"], r["withdrawn"], r["action"]) == (None, True, "withdrawn")


def test_numbers_keep_their_sign_and_every_decimal():
    assert crisil.number("81,342") == Decimal("81342")
    assert crisil.number("(1.2)") == Decimal("-1.2")
    assert crisil.number("1092.9316") == Decimal("1092.9316")
    assert crisil.number("NA") is None and crisil.number("--") is None


# --- the rating-action tables ----------------------------------------------------

def test_every_action_row_is_a_rating_with_its_class_and_qualifiers(stored):
    actions = sorted((r.instrument, r.instrument_class, r.term, r.rating, r.outlook,
                      r.rated_amount_cr, tuple(r.qualifiers))
                     for r in ratings(stored) if r.rated_amount_cr is not None)
    assert actions == sorted([
        ("Bank Loan Facilities", "bank_facility", "long_term", "AA", "Stable",
         Decimal("7000"), ()),
        ("Non Convertible Debentures", "ncd", "long_term", "AA", "Stable",
         Decimal("4666.31"), ("interchangeable_subordinated",)),
        ("Non Convertible Debentures", "ncd", "long_term", "AA", "Stable",
         Decimal("4346.57"), ("interchangeable_subordinated", "retail")),
        ("Long Term Principal Protected Market Linked Debentures", "mld", "long_term", "AA",
         "Stable", Decimal("859"), ("ppmld",)),
        ("Perpetual Bonds", "perpetual_debt", "long_term", "AA-", "Stable", Decimal("300"), ()),
        ("Commercial Paper Programme (IPO Financing)", "commercial_paper", "short_term",
         "A1+", None, Decimal("500"), ()),
        ("Commercial Paper", "commercial_paper", "short_term", "A1+", None, Decimal("8500"), ()),
    ])
    assert all(r.action == "reaffirmed" and r.action_date == date(2026, 1, 27)
               for r in ratings(stored) if r.rated_amount_cr is not None)


def test_a_footnote_symbol_means_what_its_own_table_says():
    """& is "interchangeable" under the action table, "retail" in the annexure."""
    assert crisil.qualifiers_from(["&"], {"&": "Interchangeable between secured and "
                                                "subordinated debt"}) == [
        "interchangeable_subordinated"]
    assert crisil.qualifiers_from(["&"], {"&": "For retail bond issuance"}) == ["retail"]


def test_annexure_markers_are_read_against_the_annexure_footnote(stored):
    sub = [r for r in ratings(stored) if r.instrument_class == "subordinated_debt"
           and r.rating is not None]
    by_marks = collections.Counter(tuple(r.qualifiers) for r in sub)
    # "Subordinated NCD#" (9) and "Subordinated NCD&#" (3): # = interchangeable, & = retail
    assert by_marks == {("interchangeable_subordinated",): 9,
                        ("retail", "interchangeable_subordinated"): 3}
    assert {(r.rating, r.outlook) for r in sub} == {("AA", "Stable")}


# --- the annexures ---------------------------------------------------------------

def instruments(stored):
    return [c for c in stored["claims"] if c.fact_key.startswith("instrument|")]


def test_every_instrument_annexure_row_is_a_claim(stored):
    rows = instruments(stored)
    assert len(rows) == 92                                   # repeated ISINs kept apart
    isins = collections.Counter(c.fact_key.split("|")[1] for c in rows)
    assert isins["NA"] == 36
    assert max(n for isin, n in isins.items() if isin != "NA") > 1


def test_open_ended_maturities_are_no_date_not_a_guess(stored):
    odd = [c for c in instruments(stored) if c.debt.maturity_date is None]
    assert {c.fact_key.split("|")[2] for c in odd} >= {"7-365 days"}


def test_withdrawn_instruments_are_withdrawals(stored):
    gone = [r for r in ratings(stored) if r.rating is None]
    assert len(gone) == 5 and all(r.action == "withdrawn" for r in gone)
    assert collections.Counter(r.instrument_class for r in gone) == {
        "ncd": 3, "subordinated_debt": 2}


# --- the rating history ------------------------------------------------------------

def history(stored):
    return [c.history for c in stored["claims"] if c.history]


def test_the_history_reads_every_dated_pair_and_skips_the_current_rating(stored):
    h = history(stored)
    assert len(h) == 129
    assert date(2026, 1, 27) not in {e.action_date for e in h}     # this document's own
    assert min(e.action_date for e in h) == date(2023, 1, 6)
    assert max(e.action_date for e in h) == date(2025, 10, 30)


def test_continuation_rows_belong_to_the_instrument_above_them(stored):
    cp = {e.instrument_class for e in history(stored) if e.instrument == "Commercial Paper"}
    assert cp == {"commercial_paper"}
    fund = [e for e in history(stored) if e.instrument == "Fund Based Facilities"]
    assert len(fund) == 22 and {e.instrument_class for e in fund} == {"bank_facility"}


def test_history_watches_and_legacy_suffixes_normalise(stored):
    h = history(stored)
    assert {e.watch for e in h if e.watch} == {"Watch with Developing Implications"}
    raw = {c.value_text: c.history.grade for c in stored["claims"] if c.history}
    assert raw["Crisil PPMLD AA r /Stable"] == "AA"


# --- key rating drivers ----------------------------------------------------------------

def test_key_rating_drivers_are_the_headlines_not_the_body(stored):
    drivers = sorted((c.fact_key.split("|")[2], c.value_text) for c in stored["claims"]
                     if c.fact_key.startswith("rationale|CRISIL|"))
    assert drivers == [
        ("strengths", "Comfortable capitalisation, supported by demonstrated ability to raise "
                      "capital and an asset-light business model"),
        ("strengths", "Established track record of operations and extensive branch network"),
        ("weaknesses", "Improvement in profitability to be demonstrated; ability to reduce "
                       "credit costs remains monitorable"),
        ("weaknesses", "Limited diversity in resource profile with comparatively higher cost of "
                       "funds; ability to diversify the borrowing base while reducing cost of "
                       "funds monitorable"),
    ]


# --- financial indicators: adjusted, never compared with reported ------------------

def test_key_financial_indicators_are_agency_adjusted(stored):
    kfi = [c for c in stored["claims"] if c.claim_type == "financial_indicator"]
    assert len(kfi) == 36
    assert {c.basis for c in kfi} == {"agency_adjusted"}
    total = next(c for c in kfi if c.fact_key == "financial|consolidated|total_assets|2025-12-31")
    assert total.value_numeric == Decimal("81342") and total.value_unit == "INR_CRORE"
    assert "adjusted" in total.anchors[-1].evidence_text          # the heading says so
    assert {c.value_unit for c in kfi} == {"INR_CRORE", "PERCENT", "TIMES"}


def test_adjusted_and_reported_under_one_key_are_never_compared():
    with connect() as conn, conn.transaction() as tx:
        issuer = conn.execute("INSERT INTO issuer (name) VALUES ('Adjusted fixture') "
                              "RETURNING id").fetchone()["id"]
        docs = []
        for source, sha in (("Company", "fixture-a"), ("CRISIL", "fixture-b")):
            docs.append(conn.execute(
                """
                INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                      byte_size, local_path, retrieved_at, page_count)
                VALUES (%s,'annual_report',%s,'t','u',%s,1,'-',now(),1) RETURNING id
                """, (issuer, source, sha)).fetchone()["id"])
        for doc, basis in zip(docs, ("consolidated", "agency_adjusted")):
            conn.execute(
                """
                INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                                   value_numeric, value_unit, basis, extractor,
                                   extractor_version)
                VALUES (%s,%s,'financial_indicator','financial|consolidated|total_assets|x',
                        's', 100, 'INR_CRORE', %s, 'fixture', '1')
                """, (issuer, doc, basis))
        kept, skipped = _comparable_keys(conn, issuer, "value_numeric")
        assert kept == []
        assert skipped == [("financial|consolidated|total_assets|x", ADJUSTED_VS_REPORTED)]
        raise psycopg.Rollback(tx)


# --- coverage --------------------------------------------------------------------------

def test_the_reference_document_meets_its_declaration(stored):
    cov = coverage.check(crisil, stored["meta"], stored["units"], stored["claims"])
    assert cov.complete, cov.missing
    assert stored["report"]["unreadable"] == []


def test_a_document_without_its_history_table_is_incomplete_and_says_why(stored):
    history_table = next(p for p, rows in crisil.grids(stored["units"]).items()
                         if crisil._is_history(rows))
    cut = [u for u in stored["units"] if not u["node_path"].startswith(history_table)]
    claims = crisil.extract(stored["meta"], cut)
    cov = coverage.check(crisil, stored["meta"], cut, claims)
    assert cov.missing == ["CRISIL: no rating history annexure entries found"]


def test_an_unreadable_action_rating_names_its_row(stored):
    units = [dict(u) for u in stored["units"]]
    target = next(u for u in units if re.fullmatch(r"\s*Crisil AA-/Stable \(Reaffirmed\)\s*",
                                                   u["text"]) and u["node_path"].endswith("td[2]"))
    target["text"] = "Rating to be confirmed"
    claims = crisil.extract(stored["meta"], units)
    cov = coverage.check(crisil, stored["meta"], units, claims)
    assert not cov.complete
    assert any("yielded no rating" in m for m in cov.missing)


# --- loaded, and shown as the policy allows ------------------------------------------

def test_the_loaded_document_is_complete_and_anchored_in_html(stored):
    doc = one("SELECT extraction_status, media_type FROM document WHERE id = %s",
              (stored["id"],))
    assert doc == {"extraction_status": "complete", "media_type": "text/html"}
    kinds = query("SELECT DISTINCT a.kind FROM evidence_anchor a WHERE a.document_id = %s",
                  (stored["id"],))
    assert kinds == [{"kind": "html"}]


def test_crisil_evidence_leaves_as_a_short_quote_and_a_link(stored):
    claim_id = one("SELECT c.id FROM claim c WHERE c.document_id = %s AND "
                   "c.fact_key = 'liquidity_detail|CRISIL'", (stored["id"],))["id"]
    shown = asgi.get(f"/api/claim/{claim_id}").json()
    assert shown["evidence_policy"] == "quote_and_link"
    assert len(shown["anchors"][0]["evidence_text"]) <= evidence.QUOTE_CAP + 1
    assert "#:~:text=" in shown["source_link"]
    assert asgi.get(f"/api/page.png?document_id={stored['id']}&page_no=1").status == 404
    for c in api.conflicts(include_resolved=True):
        for m in c["members"]:
            if m["source_name"] == "CRISIL" and m["value_text"]:
                assert len(m["value_text"]) <= evidence.QUOTE_CAP + 1


def test_crisil_disagreements_start_where_the_documents_say():
    """With Crisil's 30 Sep 2024 rationale loaded (and its history filling the
    months between), each disagreement starts when both views were first in
    force, not at the one Crisil document first loaded; the rows keyed on
    27 Jan 2026 are kept, resolved."""
    open_rows = {r["fact_key"] for r in query(
        "SELECT fact_key FROM conflict WHERE fact_key LIKE '%%CRISIL%%' AND resolved_at IS NULL")}
    if not open_rows:
        pytest.skip("Crisil not reconciled")
    # (no CARE pair: CARE withdrew its last IIFL ratings on 20 Sep 2024)
    assert open_rows == {
        "rating_grade|ncd|long_term|Brickwork~CRISIL|2025-09-15",
        "rating_grade|perpetual_debt|long_term|Brickwork~CRISIL|2025-09-15",
        "rating_outlook|bank_facility|long_term|CRISIL~ICRA|2025-09-24",
        "rating_outlook|ncd|long_term|CRISIL~ICRA|2025-09-24",
        "rating_outlook|subordinated_debt|long_term|CRISIL~ICRA|2025-09-24",
    }
    resolved = {r["fact_key"] for r in query(
        "SELECT fact_key FROM conflict WHERE fact_key LIKE '%%CRISIL%%|2026-01-27' "
        "AND resolved_at IS NOT NULL")}
    assert len(resolved) == 7
