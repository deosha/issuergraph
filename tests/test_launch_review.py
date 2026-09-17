"""The five pre-launch findings, each locked down.

  1. demo-only mode must close the live API, not just the /app page
  2. a withdrawn rating is a terminal state on its tranche
  3. a rating rationale is complete only if each essential section parsed,
     and an absence in an incomplete report is not a "removal"
  4. a rating disagreement the sources ended is not open
  5. the pilot form's rate-limit identity cannot be chosen by the caller
"""
from __future__ import annotations

from datetime import date

import pytest

from issuergraph import pilot
from issuergraph.db import connect, query
from issuergraph.effective import _states, is_withdrawal, overlaps
from issuergraph.extractors import icra
from issuergraph.extractors.coverage import check
from issuergraph.site import client_ip, public_path

import asgi


# --- 1. demo-only closes the live API ----------------------------------------

LIVE_ROUTES = ("/api/issuers", "/api/issuer", "/api/debt", "/api/ratings",
               "/api/rating-timeline", "/api/conflicts", "/api/corroborations",
               "/api/changes", "/api/claim/1", "/api/pdf/1",
               "/api/page.png?document_id=1&page_no=1")


@pytest.mark.parametrize("route", LIVE_ROUTES)
def test_demo_only_refuses_every_live_route(monkeypatch, route):
    monkeypatch.setenv("ISSUERGRAPH_DEMO_ONLY", "1")
    response = asgi.get(route)
    assert response.status == 404, route
    assert "not served by this deployment" in response.text


def test_demo_only_still_serves_the_public_routes(monkeypatch):
    monkeypatch.setenv("ISSUERGRAPH_DEMO_ONLY", "1")
    assert asgi.get("/api/config").status == 200
    assert asgi.get("/api/demo/overview").status == 200
    assert asgi.get("/static/site.css").status == 200
    assert asgi.get("/healthz").status == 200


def test_the_gate_is_deny_by_default():
    """A live route added tomorrow is closed without anyone remembering to list it."""
    assert not public_path("/api/anything-new")
    assert not public_path("/api/demo")            # the prefix needs its slash
    assert public_path("/api/demo/overview")
    assert public_path("/api/pilot")
    assert public_path("/")


def test_the_live_api_is_open_when_not_demo_only():
    assert asgi.get("/api/issuers").status == 200


# --- 2. withdrawal is terminal -----------------------------------------------

@pytest.mark.parametrize("action,expected", [
    ("reaffirmed; withdrawn", True),
    ("reaffirmed and withdrawn", True),
    ("Withdrawn", True),
    ("reaffirmed", False),
    ("placed on", False),
    (None, False),
])
def test_withdrawal_is_read_from_the_action(action, expected):
    assert is_withdrawal(action) is expected


def _row(claim_id, action, day, instrument="Commercial paper programme"):
    return {"claim_id": claim_id, "agency": "ICRA", "instrument": instrument,
            "instrument_class": "commercial_paper", "term": "short_term",
            "grade": "A1+", "outlook": None, "watch": None,
            "action": action, "action_date": day}


def test_a_withdrawn_tranche_ends_on_its_action_date():
    states = _states([_row(1, "reaffirmed", date(2025, 9, 24)),
                      _row(2, "reaffirmed; withdrawn", date(2026, 2, 11)),
                      _row(3, "reaffirmed", date(2026, 2, 11))])
    by_id = {s["claim_id"]: s for s in states}
    assert by_id[1]["effective_to"] == date(2026, 2, 11)
    assert by_id[2]["withdrawn"] and by_id[2]["effective_to"] == date(2026, 2, 11)
    # the sibling tranche reaffirmed the same day stays rated
    assert not by_id[3]["withdrawn"] and by_id[3]["effective_to"] is None


def test_a_withdrawn_state_overlaps_nothing():
    withdrawn = _states([_row(1, "reaffirmed; withdrawn", date(2026, 2, 11))])[0]
    other = {"effective_from": date(2025, 1, 1), "effective_to": None}
    assert overlaps(withdrawn, other) is None


def test_the_corpus_marks_icras_withdrawn_ipo_programme():
    rows = query(
        """
        SELECT withdrawn, effective_from, effective_to FROM rating_state
        WHERE agency = 'ICRA' AND instrument = 'Commercial paper programme (IPO financing)'
        ORDER BY effective_from
        """
    )
    if not rows:
        pytest.skip("corpus not loaded")
    latest = rows[-1]
    assert latest["withdrawn"] is True
    assert latest["effective_to"] == latest["effective_from"]
    assert not any(r["effective_to"] is None for r in rows), "no state is open-ended"


def test_the_timeline_api_does_not_call_a_withdrawn_rating_current():
    rows = asgi.get("/api/rating-timeline").json()
    for row in rows:
        if row["withdrawn"]:
            assert row["current"] is False


# --- 3. coverage per section -------------------------------------------------

@pytest.fixture(scope="module")
def icra_pages():
    rows = query(
        """
        SELECT page_no, text FROM document_page p JOIN document d ON d.id = p.document_id
        WHERE d.source_name = 'ICRA' AND d.published_date = '2025-09-24'
        ORDER BY page_no
        """
    )
    if not rows:
        pytest.skip("ICRA September 2025 rationale not ingested")
    return [dict(r) for r in rows]


def mutate(pages, old, new):
    return [{"page_no": p["page_no"], "text": p["text"].replace(old, new)} for p in pages]


def test_the_icra_rationale_extracts_completely(icra_pages):
    claims = icra.extract(_meta(), icra_pages)
    assert check(icra, None, icra_pages, claims).complete


def test_a_renamed_summary_heading_is_incomplete_not_complete(icra_pages):
    """The reviewer's reproduction: 87 claims, zero ratings, status 'complete'."""
    moved = mutate(mutate(icra_pages, "Summary of rating action", "Rating summary"),
                   "Summary of rating(s) outstanding", "Ratings outstanding")
    claims = icra.extract(_meta(), moved)
    assert claims, "bullets and liquidity still parse"
    assert not any(c.claim_type == "rating" for c in claims)
    cov = check(icra, None, moved, claims)
    assert not cov.complete
    assert any("no rating found" in m for m in cov.missing)


def test_each_essential_section_is_its_own_requirement(icra_pages):
    gone = mutate(icra_pages, "Liquidity position:", "Cash position:")
    cov = check(icra, None, gone, icra.extract(_meta(), gone))
    assert any("liquidity" in m for m in cov.missing)
    assert not any("no rating found" in m for m in cov.missing)


def test_a_material_event_release_requires_only_the_table():
    rows = query(
        """
        SELECT page_no, text FROM document_page p JOIN document d ON d.id = p.document_id
        WHERE d.source_name = 'ICRA' AND d.published_date = '2024-03-12'
        ORDER BY page_no
        """
    )
    if not rows:
        pytest.skip("ICRA March 2024 release not ingested")
    pages = [dict(r) for r in rows]
    assert icra.is_material_event_release(pages)
    cov = check(icra, None, pages, icra.extract(_meta(date(2024, 3, 12)), pages))
    assert cov.complete and cov.expected == 1


def test_every_rating_extractor_declares_coverage():
    from issuergraph.extractors import brickwork, care
    for module in (icra, care, brickwork):
        assert hasattr(module, "check_coverage"), module.__name__


def test_an_absence_from_an_incomplete_report_is_unconfirmed():
    """Flip one report to incomplete, rebuild the diff, and every entry that
    rests on that report lacking something must say so."""
    from issuergraph.diff import build_diffs
    doc = query("SELECT id, issuer_id FROM document WHERE source_name = 'ICRA' "
                "AND published_date = '2026-02-11'")
    if not doc:
        pytest.skip("corpus not loaded")
    doc_id, issuer_id = doc[0]["id"], doc[0]["issuer_id"]
    with connect() as conn:
        try:
            conn.execute("UPDATE document SET extraction_status = 'incomplete', "
                         "extraction_missing = ARRAY['test: simulated shortfall'] "
                         "WHERE id = %s", (doc_id,))
            build_diffs(conn, issuer_id)
            rows = conn.execute(
                "SELECT direction, certainty FROM rationale_diff "
                "WHERE to_document_id = %s", (doc_id,)).fetchall()
            removed = [r for r in rows if r["direction"] == "removed"]
            assert removed, "the Feb 2026 report drops the MLD rating"
            assert all(r["certainty"] == "unconfirmed" for r in removed)
            assert all(r["certainty"] == "confirmed"
                       for r in rows if r["direction"] == "changed")
        finally:
            conn.rollback()


def test_the_corpus_diffs_are_confirmed():
    rows = query("SELECT certainty FROM rationale_diff")
    if rows:
        assert {r["certainty"] for r in rows} == {"confirmed"}


def _meta(published=date(2025, 9, 24)):
    class Meta:
        published_date = published
    return Meta()


# --- 4. ended disagreements are not open -------------------------------------

def test_a_closed_window_records_when_it_ended():
    rows = query("SELECT fact_key, subject, ended_on, resolved_at FROM conflict "
                 "WHERE fact_key LIKE 'rating_watch|%%|CARE~ICRA|%%'")
    if not rows:
        pytest.skip("corpus not loaded")
    for row in rows:
        assert row["ended_on"] == date(2025, 9, 24), row["fact_key"]
        assert row["resolved_at"] is None       # still detected; the rows are history


def test_the_conflicts_api_separates_open_from_ended():
    open_rows = asgi.get("/api/conflicts").json()
    assert all(r["status"] == "open" and r["ended_on"] is None for r in open_rows)
    everything = asgi.get("/api/conflicts?include_resolved=true").json()
    ended = [r for r in everything if r["status"] == "ended"]
    assert len(ended) == 2
    assert len(open_rows) == len(everything) - len(ended)


def test_an_open_rating_conflict_is_still_in_force():
    for row in asgi.get("/api/conflicts").json():
        if row["kind"] == "categorical_disagreement":
            assert "still in force" in row["subject"]


# --- 5. rate-limit identity --------------------------------------------------

class FakeRequest:
    def __init__(self, peer, forwarded=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


def test_with_no_trusted_proxy_the_header_is_ignored(monkeypatch):
    monkeypatch.setenv("ISSUERGRAPH_TRUSTED_PROXY_HOPS", "0")
    assert client_ip(FakeRequest("10.0.0.5", "203.0.113.9")) == "10.0.0.5"


def test_behind_one_proxy_the_appended_value_wins(monkeypatch):
    monkeypatch.setenv("ISSUERGRAPH_TRUSTED_PROXY_HOPS", "1")
    # the caller claimed 1.1.1.1; the proxy appended the real peer
    assert client_ip(FakeRequest("10.0.0.5", "1.1.1.1, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(FakeRequest("10.0.0.5", "203.0.113.9")) == "203.0.113.9"


def test_a_missing_chain_falls_back_to_the_peer(monkeypatch):
    monkeypatch.setenv("ISSUERGRAPH_TRUSTED_PROXY_HOPS", "1")
    assert client_ip(FakeRequest("10.0.0.5")) == "10.0.0.5"


def test_changing_the_header_does_not_change_the_throttle_identity(monkeypatch):
    monkeypatch.setattr(pilot, "rate_limit", lambda: (2, 3600))
    monkeypatch.setenv("ISSUERGRAPH_TRUSTED_PROXY_HOPS", "0")
    with connect() as conn:
        conn.execute("DELETE FROM pilot_submission_log")
        conn.execute("DELETE FROM pilot_request WHERE email_key LIKE '%%.test'")
        conn.commit()
    body = {"name": "A Tester", "email": "spoof{}@examplecredit.test",
            "organisation": "Example Credit", "role": "analyst",
            "issuers": "One, Two, Three", "workflow": "quarterly surveillance of NBFCs"}
    try:
        for n in range(2):
            r = asgi.post("/api/pilot", json={**body, "email": body["email"].format(n)},
                          headers={"X-Forwarded-For": f"198.51.100.{n}"})
            assert r.status == 200
        r = asgi.post("/api/pilot", json={**body, "email": body["email"].format(9)},
                      headers={"X-Forwarded-For": "198.51.100.99"})
        assert r.status == 429
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM pilot_submission_log")
            conn.execute("DELETE FROM pilot_request WHERE email_key LIKE '%%.test'")
            conn.commit()
