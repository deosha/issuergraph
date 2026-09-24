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
from decimal import Decimal

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

def _visible(card):
    return [(side["agency"], a) for side in card["evidence"] for a in side["actions"]]


def test_a_difference_card_opens_with_the_action_in_force_when_it_began():
    """Per agency: the action in force at the start of the overlap first, then
    its later actions in date order — never one superseded before it began."""
    card = next(c for c in api.conflicts()
                if c["fact_key"] == "rating_grade|ncd|long_term|Brickwork~ICRA|2025-09-15")
    icra = [a for agency, a in _visible(card) if agency == "ICRA"]
    assert icra[0]["published_date"] == date(2024, 9, 25)
    later = [a["first"] for a in icra[1:]]
    assert later == sorted(later) and all(d > date(2025, 9, 15) for d in later)


def test_difference_cards_stay_short():
    """At most six rows before anything is expanded; the rest is history or
    an instrument list, one click away."""
    for c in api.conflicts(include_resolved=True):
        assert len(_visible(c)) <= 6, (c["fact_key"], len(_visible(c)))


def test_no_evidence_row_repeats_on_a_card():
    for c in api.conflicts(include_resolved=True):
        rows = [(agency, a["document_id"], a["stated_value"]) for agency, a in _visible(c)]
        rows += [(s["agency"], a["document_id"], a["stated_value"])
                 for s in c["evidence"] for a in s["history"]]
        assert len(rows) == len(set(rows)), c["fact_key"]
        for _, a in _visible(c):
            quotes = [" ".join((q["text"] or "").split()) for q in a["quotes"]]
            assert len(quotes) == len(set(quotes)), c["fact_key"]


def test_one_publishers_restatements_are_one_source():
    """Brickwork's three rationales + the company, within tolerance: two sources
    agree. Brickwork's three alone: a single source."""
    rows = {(r["basis"], r["as_of_date"]): r for r in api.debt()["rows"]}
    both = rows[("consolidated", date(2025, 3, 31))]
    assert both["agreement"]["label"].startswith("2 sources agree")
    bwr = next(c for c in both["cells"] if c["source_name"] == "Brickwork")
    assert len(bwr["statements"]) == 3 and len({s["claim_id"] for s in bwr["statements"]}) == 3
    alone = rows[("consolidated", date(2023, 3, 31))]
    assert [c["source_name"] for c in alone["cells"]] == ["Brickwork"]
    assert len(alone["cells"][0]["statements"]) == 3
    assert alone["agreement"] == {"kind": "single", "label": "single source"}
    assert rows[("consolidated", date(2024, 3, 31))]["agreement"]["label"].startswith("difference ")


def test_debt_agreement_counts_publishers_not_documents():
    from issuergraph.api import _debt_rows

    def t(src, value, doc):
        return {"basis": "consolidated", "as_of_date": date(2025, 3, 31), "source_name": src,
                "value_numeric": Decimal(value), "value_unit": "INR_CRORE", "claim_id": doc,
                "title": f"doc {doc}", "published_date": date(2025, doc, 1), "conflict": None}
    three = [t("Brickwork", "50000", 1), t("Brickwork", "50000", 2), t("Brickwork", "50000", 3)]
    assert _debt_rows(three)[0]["agreement"]["label"] == "single source"
    row = _debt_rows([*three, t("Company", "50000.5", 4)])[0]
    assert row["agreement"]["label"] == "2 sources agree ±₹0.5 crore"
    changed = _debt_rows([t("Brickwork", "100", 1), t("Brickwork", "120", 2)])[0]["cells"][0]
    assert changed["changed_within"] and changed["value_numeric"] == Decimal("120")


def test_the_ui_uses_facts_and_differences_and_hides_internal_keys():
    js = (STATIC / "app.js").read_text()
    assert '["conflicts", "Differences"]' in js
    assert "anchored claims" not in js and "open conflicts" not in js
    assert "conflict ${" not in js and "agreeBadge(r.agreement)" in js
    # the key is a data attribute; shown only with ?debug=1
    assert 'data-fact-key="${esc(c.fact_key)}"' in js
    assert re.search(r"DEBUG\s*\n?\s*\?\s*` <code>\$\{esc\(c\.fact_key\)\}</code>`", js)
    assert 'INR_CRORE: "₹ crore"' in js
    assert '"publication date not stated"' in js
    css = (STATIC / "app.css").read_text()
    assert ".pill.diff" in css and ".card.conflict { border-color: var(--line); }" in css


# --- evidence panel ------------------------------------------------------------

def _crisil_claim():
    from issuergraph.db import one
    row = one("SELECT c.id FROM claim c JOIN document d ON d.id = c.document_id "
              "WHERE d.source_name = 'CRISIL' AND c.fact_key LIKE 'rating_instrument|%%' "
              "ORDER BY c.id LIMIT 1")
    if not row:
        pytest.skip("Crisil not loaded")
    return api.claim(row["id"])


def test_a_web_page_panel_names_no_page_and_no_placeholder():
    from issuergraph import evidence
    c = _crisil_claim()
    text = " ".join(c["panel_lines"])
    assert "web page · chars " in text and "page 0" not in text and " of 0" not in text
    assert "basis" not in text                    # a rating has no accounting basis
    assert not any(evidence.placeholder_in(line) for line in c["panel_lines"])
    assert c["policy_note"] == ("Crisil's terms restrict redistribution, so this is shown "
                                "as a short quote. Open it on Crisil's page to read it in "
                                "context.")
    assert "#:~:text=" in c["source_link"] and "#" not in c["plain_url"]


def test_the_snapshot_build_refuses_a_placeholder_in_a_panel():
    from scripts.export_demo import panel_guard
    assert panel_guard({"1": {"panel_lines": ["web page · basis unknown"]}}) == [
        "claim 1: 'unknown' in 'web page · basis unknown'"]
    assert panel_guard({"1": {"panel_lines": ["title · None"]}})
    assert panel_guard({"1": {"panel_lines": ["web page · chars 4–93"]}}) == []


def test_a_pdf_panel_keeps_its_page_and_basis():
    from issuergraph.evidence import panel_lines
    row = {"title": "AR", "source_name": "Company", "published_date": None,
           "basis": "consolidated", "as_of_date": date(2024, 3, 31), "page_count": 300,
           "anchors": [{"kind": "pdf", "page_no": 12, "char_start": 1, "char_end": 9}]}
    assert panel_lines(row) == ["AR · Company · publication date not stated",
                                "page 12 of 300 · chars 1–9 · basis consolidated · "
                                "as at 31 Mar 2024"]


# --- wording -------------------------------------------------------------------

def test_instrument_names_read_once_and_say_what_they_are():
    from issuergraph.wording import display_name
    assert display_name("Long Term Long Term Instruments", "subordinated_debt") == \
        "Long Term Instruments (subordinated debt)"
    assert display_name("Non-convertible debenture programme", "ncd") == \
        "Non-convertible debenture programme"


def test_actions_read_as_sentences():
    from issuergraph.wording import action_text
    assert action_text("assigned; downgraded; removed", "AA-", "Stable", None, "AA") == \
        "downgraded to AA- from AA, removed from watch, Stable outlook assigned"
    assert action_text("revised", "AA", None, "Watch with Negative Implications", None) == \
        "watch revised to negative implications"
    assert action_text("reaffirmed; withdrawn", "A1+", None, None, None) == \
        "reaffirmed at A1+, withdrawn"
    assert all(r["action_text"] and "None" not in r["action_text"] for r in api.ratings())


# --- site ------------------------------------------------------------------------

def test_scripts_are_versioned_and_revalidated():
    from issuergraph.site import ASSET_VERSION
    body = asgi.get("/demo").text
    assert f'/static/app.js?v={ASSET_VERSION}"' in body and "{{asset_version}}" not in body
    r = asgi.get("/static/app.js")
    assert r.headers.get("cache-control") == "no-cache"


def test_sitemap_lists_the_public_pages():
    from issuergraph.settings import site_url
    r = asgi.get("/sitemap.xml")
    assert r.status == 200 and "xml" in r.headers["content-type"]
    for path in ("/", "/demo", "/pilot", "/privacy"):
        assert f"<loc>{site_url()}{path}</loc>" in r.text
    assert "/app" not in r.text
    assert f"Sitemap: {site_url()}/sitemap.xml" in asgi.get("/robots.txt").text


def test_the_privacy_notice_lists_exactly_the_events_the_site_sends():
    from issuergraph.site import ANALYTICS_EVENTS
    sent = set()
    for path in STATIC.glob("*.*"):
        if path.suffix not in (".js", ".html"):
            continue
        text = path.read_text()
        sent |= set(re.findall(r'\btrack\(\s*"([a-z_]+)"', text))
        sent |= set(re.findall(r'"([a-z_]+_cta_clicked)"', text))
    assert sent == set(ANALYTICS_EVENTS)
    body = asgi.get("/privacy").text
    for name in ANALYTICS_EVENTS:
        assert f"<code>{name}</code>" in body


def test_headings_read_dates_and_names_as_people_write_them():
    from issuergraph.wording import readable
    assert readable("CRISIL says AA — both in force from 2025-09-15 (still in force)") == \
        "Crisil says AA — both in force from 15 Sep 2025 (still in force)"
    for c in api.conflicts(include_resolved=True):
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\bCRISIL\b", c["subject"] + c["note"])
