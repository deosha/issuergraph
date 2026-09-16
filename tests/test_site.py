"""The public site: pages, demo snapshot, metadata and accessibility basics.

Everything here runs against the real ASGI app. The demo assertions matter most:
the demo is the product's credibility in public, so its evidence has to be
verifiable in the same way the pipeline's is — the snapshot must prove what it
displays, not merely display it.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

import asgi
from issuergraph import demo

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
PAGES = {"/": "index.html", "/demo": "demo.html", "/pilot": "pilot.html"}


# --- pages ------------------------------------------------------------------

@pytest.mark.parametrize("path", list(PAGES))
def test_page_is_served(path):
    response = asgi.get(path)
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_has_title_description_and_sharing_metadata(name):
    html = (STATIC / name).read_text()
    assert re.search(r"<html lang=\"en\">", html)
    assert re.search(r"<title>[^<]{15,}</title>", html), "a title that says something"
    assert re.search(r'name="description" content="[^"]{60,}"', html)
    assert re.search(r'property="og:title"', html)
    assert re.search(r'property="og:image"', html)
    assert re.search(r'name="twitter:card"', html)


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_every_page_has_a_skip_link(name):
    html = (STATIC / name).read_text()
    assert 'class="skip"' in html, "keyboard users need a way past the header"


def test_every_form_input_has_a_label():
    html = (STATIC / "pilot.html").read_text()
    ids = re.findall(r'<(?:input|textarea)[^>]*\bid="([^"]+)"', html)
    labelled = set(re.findall(r'<label for="([^"]+)"', html))
    assert ids, "the form should have fields"
    assert set(ids) <= labelled, f"unlabelled fields: {set(ids) - labelled}"


def test_required_fields_are_marked_for_assistive_tech():
    html = (STATIC / "pilot.html").read_text()
    for field in ("name", "email", "organisation", "role", "issuers", "workflow"):
        block = re.search(rf'<(?:input|textarea) id="{field}".*?>', html, re.S)
        assert block and "required" in block.group(0), field
    notes = re.search(r'<textarea id="notes".*?>', html, re.S).group(0)
    assert "required" not in notes, "the optional field must not be required"
    assert "<span class=\"optional\">(optional)</span>" in html


def test_the_product_view_is_not_indexed():
    assert 'name="robots" content="noindex"' in (STATIC / "app.html").read_text()
    assert "Disallow: /app" in asgi.get("/robots.txt").text


# --- configuration ----------------------------------------------------------

def test_config_is_public_by_construction():
    config = asgi.get("/api/config").json()
    assert config["contact_email"]
    blob = json.dumps(config).lower()
    for secret in ("token", "secret", "password", "dsn", "webhook"):
        assert secret not in blob, f"/api/config leaked something called {secret}"


def test_analytics_is_off_until_a_key_is_configured():
    """No key, no loader, no request to a third party."""
    config = asgi.get("/api/config").json()
    assert config["analytics"]["enabled"] is False
    assert config["analytics"]["key"] == ""


def test_pricing_defaults_to_asking_for_scope():
    assert asgi.get("/api/config").json()["pilot_price"] == ""
    assert "Pricing is scoped per pilot" in (STATIC / "index.html").read_text()


# --- the demo snapshot ------------------------------------------------------

def test_overview_counts_come_from_the_data():
    data = asgi.get("/api/demo/overview").json()
    counts = data["counts"]
    assert counts["documents"] == len(data["issuer"]["documents"])
    assert counts["changes"] == len(data["changes"])
    assert counts["conflicts_open"] == sum(1 for c in data["conflicts"]
                                           if c["status"] == "open")
    assert counts["conflicts_resolved"] == sum(1 for c in data["conflicts"]
                                               if c["status"] == "resolved")


def test_the_demo_states_its_cutoff():
    data = asgi.get("/api/demo/overview").json()
    assert data["sample"] is True
    assert data["document_cutoff"], "a sample with no date invites being read as current"
    assert data["document_cutoff"] >= max(
        d["published_date"] for d in data["issuer"]["documents"] if d["published_date"])


def test_demo_claims_carry_verifiable_evidence():
    """Offsets, quoted text and geometry all survive the export."""
    snapshot = demo.snapshot()
    assert snapshot["claims"], "no claims in the snapshot"
    for claim_id, claim in snapshot["claims"].items():
        assert claim["anchors"], f"claim {claim_id} has no evidence"
        for anchor in claim["anchors"]:
            assert anchor["evidence_text"].strip()
            assert anchor["char_end"] > anchor["char_start"] >= 0
            assert anchor["bbox_rects"], "a highlight needs geometry"


def test_every_anchor_has_its_page_image_on_disk():
    snapshot = demo.snapshot()
    for claim in snapshot["claims"].values():
        for anchor in claim["anchors"]:
            key = f"{claim['document_id']}-{anchor['page_no']}"
            assert key in snapshot["pages"], key
            image = STATIC / "demo" / snapshot["pages"][key]["image"]
            assert image.exists() and image.stat().st_size > 1000, image


def test_highlight_rectangles_lie_inside_their_page():
    """What makes the overlay trustworthy: the geometry is in page space."""
    snapshot = demo.snapshot()
    for claim in snapshot["claims"].values():
        for anchor in claim["anchors"]:
            page = snapshot["pages"][f"{claim['document_id']}-{anchor['page_no']}"]
            for x0, y0, x1, y1 in anchor["bbox_rects"]:
                assert 0 <= x0 < x1 <= page["width"] + 1, (x0, x1, page["width"])
                assert 0 <= y0 < y1 <= page["height"] + 1, (y0, y1, page["height"])


def test_demo_claim_endpoint_returns_pages_for_its_anchors():
    overview = asgi.get("/api/demo/overview").json()
    claim_id = next(t["claim_id"] for t in overview["debt"]["totals"] if t.get("claim_id"))
    claim = asgi.get(f"/api/demo/claim/{claim_id}").json()
    assert claim["anchors"] and claim["pages"]
    for anchor in claim["anchors"]:
        assert f"{claim['document_id']}-{anchor['page_no']}" in claim["pages"]


def test_a_claim_outside_the_snapshot_is_not_served():
    response = asgi.get("/api/demo/claim/999999999")
    assert response.status == 404
    assert "snapshot" in response.json()["detail"]


def test_the_demo_exposes_only_curated_records():
    """The snapshot is the boundary: there is no route from /demo to the database."""
    snapshot = demo.snapshot()
    exported = set(snapshot["claims"])
    overview = asgi.get("/api/demo/overview").json()
    assert "local_path" not in json.dumps(overview), "no filesystem paths in public data"
    # Everything the demo can serve is in the file, and the file is a subset.
    assert len(exported) <= overview["issuer"]["claim_count"]


def test_the_tour_only_points_at_data_that_exists():
    steps = asgi.get("/api/demo/tour").json()
    overview = asgi.get("/api/demo/overview").json()
    tabs = {"debt", "ratings", "conflicts", "changes", "sources"}
    conflict_ids = {c["id"] for c in overview["conflicts"]}
    assert len(steps) >= 4
    for step in steps:
        assert step["tab"] in tabs
        assert step["title"] and step["body"]
        if "claim_id" in step:
            assert asgi.get(f"/api/demo/claim/{step['claim_id']}").status == 200
        if "conflict_id" in step:
            assert step["conflict_id"] in conflict_ids


def test_resolved_differences_are_reported_accurately():
    """Whatever the snapshot holds, the count must match — including zero."""
    overview = asgi.get("/api/demo/overview").json()
    resolved = [c for c in overview["conflicts"] if c["status"] == "resolved"]
    assert overview["counts"]["conflicts_resolved"] == len(resolved)
    for conflict in overview["conflicts"]:
        assert (conflict["resolved_at"] is None) == (conflict["status"] == "open")


def test_differences_are_not_called_errors():
    """A difference between sources is a judgement for an analyst, not a defect."""
    html = (STATIC / "index.html").read_text().lower()
    assert "difference between sources is not an error" in html
    for claim in ("error rate", "accuracy of", "% accurate", "errors found"):
        assert claim not in html


# --- analytics --------------------------------------------------------------

def test_analytics_cannot_send_personal_fields():
    """The allowlist is the enforcement point, so assert on the allowlist."""
    source = (STATIC / "analytics.js").read_text()
    allowed = set(re.findall(r'"([a-z_]+)"',
                             re.search(r"const ALLOWED = new Set\(\[(.*?)\]\)",
                                       source, re.S).group(1)))
    assert allowed, "no allowlist found"
    for forbidden in ("name", "email", "organisation", "role", "issuers", "workflow",
                      "notes", "evidence_text", "value", "subject", "text"):
        assert forbidden not in allowed, f"analytics may send {forbidden}"
    assert "autocapture: false" in source, "autocapture would collect form text"


def test_tracked_events_cover_the_funnel():
    sources = "\n".join((STATIC / name).read_text()
                        for name in ("site.js", "demo.html", "pilot.js"))
    for event in ("landing_viewed", "demo_started", "evidence_opened",
                  "sources_compared", "pilot_submitted"):
        assert event in sources, f"{event} is not tracked anywhere"


# --- demo-only deployments --------------------------------------------------

def test_demo_only_deployments_do_not_serve_the_live_product(monkeypatch):
    """A public host needs the site and the snapshot, not the database."""
    monkeypatch.setenv("ISSUERGRAPH_DEMO_ONLY", "1")
    assert asgi.get("/app").status == 404
    assert asgi.get("/").status == 200
    assert asgi.get("/demo").status == 200
    assert asgi.get("/api/demo/overview").status == 200
    assert asgi.get("/api/config").json()["demo_only"] is True


def test_the_live_product_is_served_by_default():
    assert asgi.get("/app").status == 200


def test_the_demo_needs_no_database(monkeypatch):
    """The snapshot is a file: point the DSN at nothing and the demo still works."""
    monkeypatch.setenv("ISSUERGRAPH_DSN", "postgresql:///issuergraph_does_not_exist")
    overview = asgi.get("/api/demo/overview")
    assert overview.status == 200 and overview.json()["counts"]["documents"] > 0
    claim_id = next(iter(demo.snapshot()["claims"]))
    assert asgi.get(f"/api/demo/claim/{claim_id}").status == 200


def test_counts_in_prose_are_bound_to_the_snapshot():
    """A hardcoded count is a claim that goes stale silently."""
    html = (STATIC / "index.html").read_text()
    assert 'data-count="documents"' in html
    assert 'data-count' in (STATIC / "site.js").read_text()
    demo_html = (STATIC / "demo.html").read_text()
    assert 'id="doccount"' in demo_html and 'id="snapcounts"' in demo_html
    # and no leftover fixed number describing the corpus
    assert "Six public documents" not in demo_html
