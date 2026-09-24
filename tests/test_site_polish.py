"""The public site as a crawler, a link preview and a reader see it.

Each test names the complaint it answers: evidence repeated per tranche, cards
citing an action that had been superseded, internal keys on screen, "claims"
and "conflicts" where the product says facts and differences, raw unit codes,
"—" in the HTML before scripts run, a dead LinkedIn link, relative canonical
URLs, www serving a duplicate site, and no privacy notice.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date

import pytest

from issuergraph import api
from issuergraph.site import page_values

import asgi

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


# --- www → apex, canonical URLs ------------------------------------------------

def test_www_redirects_permanently_to_the_apex_keeping_path_and_query():
    r = asgi.get("/demo?tour=1", headers={"x-forwarded-host": "www.issuergraph.com"})
    assert r.status == 301
    assert r.headers["location"] == "https://issuergraph.com/demo?tour=1"


def test_the_apex_and_other_hosts_are_not_redirected():
    assert asgi.get("/healthz", headers={"x-forwarded-host": "issuergraph.com"}).status == 200
    assert asgi.get("/healthz", headers={"x-forwarded-host": "www.example.com"}).status == 200


@pytest.mark.parametrize("path, canonical", [
    ("/", "https://issuergraph.com/"), ("/demo", "https://issuergraph.com/demo"),
    ("/pilot", "https://issuergraph.com/pilot"), ("/privacy", "https://issuergraph.com/privacy"),
])
def test_canonical_links_are_absolute(path, canonical):
    body = asgi.get(path).text
    assert f'<link rel="canonical" href="{canonical}">' in body
    assert "{{" not in body


# --- what a crawler reads before any script runs ---------------------------------

def test_the_landing_html_carries_the_founder_counts_and_cutoff():
    body = asgi.get("/").text
    values = page_values()
    assert f'data-cfg="founder_name">{values["founder_name"]}<' in body
    assert values["founder_name"] not in ("", "—")
    assert f'data-count="documents">{values["count_documents"]}<' in body
    assert f'id="cutoff-inline">{values["cutoff"]}<' in body


def test_no_link_points_nowhere():
    for path in ("/", "/pilot", "/demo", "/privacy"):
        assert 'href="#"' not in asgi.get(path).text, path


def test_linkedin_appears_only_when_configured():
    body = asgi.get("/").text
    assert ("LinkedIn" in body) == bool(page_values()["founder_linkedin_link"])


def test_the_demo_html_states_its_counts_before_scripts_run():
    body = asgi.get("/demo").text
    assert 'id="doccount">—<' not in body and 'id="cutoff">—<' not in body
    assert re.search(r'id="doccount">\d+<', body)
    assert re.search(r'og:description" content="[^"]*\d+ public documents', body)


# --- privacy ---------------------------------------------------------------------

def test_privacy_is_linked_from_the_footer_and_under_the_pilot_form():
    assert 'href="/privacy">Privacy<' in asgi.get("/").text
    pilot = asgi.get("/pilot").text
    assert 'href="/privacy">Privacy<' in pilot
    form = pilot[pilot.index("<form"):pilot.index("</form>")]
    assert 'href="/privacy"' in form


def test_privacy_states_what_this_deployment_does():
    body = " ".join(asgi.get("/privacy").text.split())
    assert "not added to a mailing list" in body
    assert "salted one-way hash" in body
    assert page_values()["privacy_analytics"] in body
    assert page_values()["privacy_crm"] in body


# --- coverage copy ---------------------------------------------------------------

def test_coverage_and_faq_name_crisil_and_its_quote_and_link_evidence():
    body = asgi.get("/").text
    coverage = body[body.index('id="coverage"'):body.index('id="pilot"')]
    assert "Crisil" in coverage and "short quote" in coverage
    assert "Five extractors" in coverage and "Four extractors" not in body
    faq = body[body.index('id="faq"'):]
    assert "ICRA, CARE, Brickwork and Crisil" in faq
    assert "six documents" not in faq


# --- difference cards ------------------------------------------------------------

def test_rating_differences_cite_the_action_in_force_in_each_period():
    card = next(c for c in api.conflicts()
                if c["fact_key"] == "rating_grade|ncd|long_term|Brickwork~ICRA|2025-09-15")
    icra = [(p["from"], s["published_date"]) for p in card["evidence"]
            for s in p["sides"] if s["source_name"] == "ICRA"]
    # never an action superseded before the period began
    assert (date(2025, 9, 15), date(2024, 9, 25)) in icra
    assert (date(2026, 2, 11), date(2026, 2, 11)) in icra
    assert all(published <= start for start, published in icra)


def test_no_evidence_row_repeats_within_a_period():
    for c in api.conflicts(include_resolved=True):
        groups = [p["sides"] for p in c["evidence"]] if c["fact_key"].startswith("rating_") \
            else [c["evidence"]]
        for sides in groups:
            keys = [(s["source_name"], s.get("document_id") or s["title"],
                     s["stated_value"], s["value_text"]) for s in sides]
            assert len(keys) == len(set(keys)), c["fact_key"]


def test_the_ui_uses_facts_and_differences_and_hides_internal_keys():
    js = (STATIC / "app.js").read_text()
    assert '["conflicts", "Differences"]' in js
    assert "anchored claims" not in js and "open conflicts" not in js
    assert "conflict ${Number" not in js and "difference ${Number" in js
    # the key is a data attribute; shown only with ?debug=1
    assert 'data-fact-key="${esc(c.fact_key)}"' in js
    assert re.search(r"DEBUG\s*\n?\s*\?\s*` <code>\$\{esc\(c\.fact_key\)\}</code>`", js)
    assert 'INR_CRORE: "₹ crore"' in js
    assert '"publication date not stated"' in js
    css = (STATIC / "app.css").read_text()
    assert ".pill.diff" in css and ".card.conflict { border-color: var(--line); }" in css
