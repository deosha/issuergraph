"""HTML evidence: anchors located by node path, verified against stored bytes.

A publisher that issues rationales as HTML (CRISIL) gives no pages and no word
geometry. An HTML anchor is a node path (lxml getpath) plus a character range in
that node's text_content(), exactly as lxml returns it. The loader re-hashes the
stored bytes, re-parses them, resolves the path and compares the quote; any
mismatch fails the whole document, as for PDFs.

The fixture is synthetic HTML written for these tests — never a publisher's
page, since committing one would itself be the redistribution the display
policy exists to prevent. It is shaped like a Word-exported rationale: CRLF
line ends, tab indentation inside cells, &nbsp;, text split across spans.
"""
from __future__ import annotations

from hashlib import sha256

import psycopg
import pytest

from issuergraph import evidence, htmldoc
from issuergraph.db import connect
from issuergraph.extractors.common import anchor_in, normalized_view, raw_span
from issuergraph.loader import EvidenceMismatch, load_claims
from issuergraph.models import Anchor, ExtractedClaim
from issuergraph.pipeline import content_type_of, pages_of

import asgi

HTML = (
    b"<html><head><title>Rating Rationale</title></head><body>\r\n"
    b"<p><span>Rating Action</span></p>\r\n"
    b"<table><tbody>\r\n"
    b"\t<tr>\r\n"
    b"\t\t<td>\r\n\t\t\t<p><span>Long Term Rating</span></p>\r\n\t\t</td>\r\n"
    b"\t\t<td>\r\n\t\t\t<p><span>Crisil</span><span>&nbsp;AA</span><span>/Stable</span>\r\n"
    b"\t\t\t(Reaffirmed)</p>\r\n\t\t</td>\r\n"
    b"\t</tr>\r\n"
    b"</tbody></table>\r\n"
    b"<p>Liquidity: <span>Adequate</span></p>\r\n"
    b"</body></html>\r\n"
)
CONTENT_TYPE = "text/html; charset=UTF-8"
CELL = "/html/body/table/tbody/tr/td[2]"
RATING = "Crisil AA/Stable (Reaffirmed)"        # as matched, whitespace collapsed


def tree():
    return htmldoc.parse(HTML, CONTENT_TYPE)


def cell_unit() -> dict:
    return next(u for u in htmldoc.units(tree()) if u["node_path"] == CELL)


def rating_anchor() -> Anchor:
    """Match on collapsed whitespace, store raw offsets — the extractor's job."""
    unit = cell_unit()
    norm, index = normalized_view(unit["text"])
    at = norm.index(RATING)
    return anchor_in(unit, *raw_span(index, at, at + len(RATING)))


def claim_with(*anchors: Anchor) -> ExtractedClaim:
    return ExtractedClaim(claim_type="rating", fact_key="fixture|rating", subject="fixture",
                          value_text="Crisil AA/Stable", extractor="fixture",
                          extractor_version="1.0.0", anchors=list(anchors))


@pytest.fixture
def conn():
    with connect() as c:
        yield c


def make_document(conn, source_name: str, data: bytes = HTML) -> dict:
    issuer_id = conn.execute(
        "INSERT INTO issuer (name) VALUES (%s) RETURNING id",
        (f"HTML Fixture Issuer {source_name}",)).fetchone()["id"]
    document_id = conn.execute(
        """
        INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256, byte_size,
                              local_path, retrieved_at, page_count, media_type, content_type)
        VALUES (%s,'rating_rationale',%s,'Fixture rationale',
                'https://publisher.example/rr/fixture.html',%s,%s,'-',now(),0,'text/html',%s)
        RETURNING id
        """,
        (issuer_id, source_name, sha256(data).hexdigest(), len(data), CONTENT_TYPE),
    ).fetchone()["id"]
    conn.execute("INSERT INTO document_blob (document_id, bytes) VALUES (%s,%s)",
                 (document_id, data))
    return {"issuer_id": issuer_id, "document_id": document_id}


@pytest.fixture
def sandbox(conn):
    """An HTML document and its bytes, rolled back after the test."""
    with conn.transaction() as tx:
        yield make_document(conn, "HTMLPub")
        raise psycopg.Rollback(tx)


@pytest.fixture
def crisil_sandbox(conn):
    with conn.transaction() as tx:
        yield make_document(conn, "CRISIL")
        raise psycopg.Rollback(tx)


def load(conn, box, *anchors):
    return load_claims(conn, box["issuer_id"], box["document_id"], [claim_with(*anchors)])


# --- what lxml gives, and what offsets index into -----------------------------

def test_offsets_index_text_content_exactly_as_lxml_returns_it():
    text = cell_unit()["text"]
    assert "\xa0" in text and "\n\t\t\t" in text          # nothing normalised away
    a = rating_anchor()
    assert text[a.char_start:a.char_end] == "Crisil\xa0AA/Stable\n\t\t\t(Reaffirmed)"
    assert (a.kind, a.node_path, a.page_no) == ("html", CELL, None)


def test_the_same_bytes_always_give_the_same_paths():
    assert [u["node_path"] for u in htmldoc.units(tree())] == \
           [u["node_path"] for u in htmldoc.units(htmldoc.parse(HTML, CONTENT_TYPE))]


def test_a_path_that_names_nothing_or_several_resolves_to_nothing():
    assert htmldoc.resolve(tree(), CELL) is not None
    assert htmldoc.resolve(tree(), "/html/body/table/tbody/tr/td[9]") is None
    assert htmldoc.resolve(tree(), "//p") is None                    # several
    assert htmldoc.resolve(tree(), "not a path[") is None


def test_the_charset_comes_from_the_content_type_header():
    assert htmldoc.charset_of("text/html; charset=UTF-8") == "utf-8"
    assert htmldoc.charset_of("text/html") is None


def test_the_last_content_type_after_redirects_is_kept(tmp_path):
    body = tmp_path / "page.html"
    body.write_bytes(HTML)
    (tmp_path / "page.html.headers").write_text(
        "HTTP/2 301\r\ncontent-type: text/html; charset=iso-8859-1\r\n\r\n"
        "HTTP/2 200\r\ncontent-type: text/html; charset=UTF-8\r\n\r\n")
    assert content_type_of(body) == "text/html; charset=UTF-8"


# --- the loader: pass ---------------------------------------------------------

def test_an_html_anchor_verifies_and_is_stored_without_geometry(conn, sandbox):
    load(conn, sandbox, rating_anchor())
    row = conn.execute(
        "SELECT kind, page_no, node_path, char_start, char_end, evidence_text, bbox "
        "FROM evidence_anchor WHERE document_id = %s", (sandbox["document_id"],)).fetchone()
    assert (row["kind"], row["page_no"], row["node_path"], row["bbox"]) == ("html", None, CELL,
                                                                           None)
    assert row["evidence_text"] == "Crisil\xa0AA/Stable\n\t\t\t(Reaffirmed)"


def test_the_extractor_reads_html_units_from_the_stored_bytes(conn, sandbox):
    units = pages_of(conn, sandbox["document_id"])
    assert CELL in {u["node_path"] for u in units}
    assert all(u["kind"] == "html" for u in units)


# --- the loader: fail ----------------------------------------------------------

def test_offsets_from_the_collapsed_text_do_not_verify(conn, sandbox):
    """The whitespace trap: the match found in the collapsed view, stored
    as-is, points three characters early in the raw text."""
    unit = cell_unit()
    norm, _ = normalized_view(unit["text"])
    at = norm.index(RATING)
    naive = Anchor(kind="html", node_path=CELL, char_start=at, char_end=at + len(RATING),
                   evidence_text=RATING)
    with pytest.raises(EvidenceMismatch, match="node  says"):
        load(conn, sandbox, naive)


def test_a_quote_that_differs_from_the_node_fails(conn, sandbox):
    a = rating_anchor()
    wrong = a.model_copy(update={"evidence_text": a.evidence_text.replace("AA", "AA+")})
    with pytest.raises(EvidenceMismatch, match="anchor says"):
        load(conn, sandbox, wrong)


def test_offsets_past_the_end_of_the_node_fail(conn, sandbox):
    a = rating_anchor()
    past = a.model_copy(update={"char_start": 500, "char_end": 510})
    with pytest.raises(EvidenceMismatch, match="past the end"):
        load(conn, sandbox, past)


@pytest.mark.parametrize("path", ["/html/body/table/tbody/tr/td[9]", "//p"])
def test_a_node_path_that_does_not_name_one_node_fails(conn, sandbox, path):
    a = rating_anchor().model_copy(update={"node_path": path})
    with pytest.raises(EvidenceMismatch, match="no single element"):
        load(conn, sandbox, a)


def test_a_changed_byte_fails_the_document_even_outside_every_anchor(conn, sandbox):
    """The stored hash covers the bytes as received."""
    tampered = HTML.replace(b"Adequate", b"Adequatf")        # not in any anchored node
    conn.execute("UPDATE document_blob SET bytes = %s WHERE document_id = %s",
                 (tampered, sandbox["document_id"]))
    with pytest.raises(EvidenceMismatch, match="no longer hash"):
        load(conn, sandbox, rating_anchor())


def test_a_changed_byte_with_a_matching_hash_still_fails_the_quote(conn, sandbox):
    """Bytes and hash both rewritten: the quote no longer matches its node."""
    tampered = HTML.replace(b"&nbsp;AA", b"&nbsp;AB")
    conn.execute("UPDATE document_blob SET bytes = %s WHERE document_id = %s",
                 (tampered, sandbox["document_id"]))
    conn.execute("UPDATE document SET sha256 = %s WHERE id = %s",
                 (sha256(tampered).hexdigest(), sandbox["document_id"]))
    with pytest.raises(EvidenceMismatch, match="anchor says"):
        load(conn, sandbox, rating_anchor())


def test_one_bad_anchor_fails_the_whole_document(conn, sandbox):
    good = claim_with(rating_anchor())
    bad = claim_with(rating_anchor().model_copy(update={"evidence_text": "Crisil AA"}))
    with pytest.raises(EvidenceMismatch):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [good, bad])
    left = conn.execute("SELECT count(*) AS n FROM claim WHERE document_id = %s",
                        (sandbox["document_id"],)).fetchone()["n"]
    assert left == 0


def test_a_whole_table_cannot_be_quoted_from_a_quote_and_link_publisher(conn, crisil_sandbox):
    whole_row = "/html/body/table/tbody/tr"
    text = htmldoc.resolve(tree(), whole_row).text_content()
    start = text.index("Long")
    a = Anchor(kind="html", node_path=whole_row, char_start=start,
               char_end=start + len("Long Term Rating"), evidence_text="Long Term Rating")
    with pytest.raises(EvidenceMismatch, match="whole-table node"):
        load(conn, crisil_sandbox, a)
    load(conn, crisil_sandbox, rating_anchor())         # the cell itself is fine


def test_each_anchor_kind_carries_exactly_its_own_locator():
    with pytest.raises(ValueError):
        Anchor(kind="html", node_path=CELL, page_no=1, char_start=0, char_end=1,
               evidence_text="x")
    with pytest.raises(ValueError):
        Anchor(kind="html", char_start=0, char_end=1, evidence_text="x")
    with pytest.raises(ValueError):
        Anchor(kind="pdf", char_start=0, char_end=1, evidence_text="x")
    with pytest.raises(ValueError):
        Anchor(kind="html", node_path=CELL, char_start=0, char_end=1, evidence_text="x",
               bbox_rects=[[0, 0, 1, 1]])


# --- what may be shown --------------------------------------------------------

def test_a_quote_is_capped_at_a_word_boundary_and_says_so():
    long = "rating " * 60
    shown, truncated = evidence.excerpt(long)
    assert truncated and len(shown) <= evidence.QUOTE_CAP + 1 and shown.endswith("…")
    assert evidence.excerpt("Crisil\xa0AA/Stable\n\t\t\t(Reaffirmed)") == (
        "Crisil AA/Stable (Reaffirmed)", False)


def test_the_source_link_carries_an_encoded_text_fragment():
    url = "https://publisher.example/rr/fixture.html#top"
    assert evidence.text_fragment(url, "Crisil\xa0AA-/Stable,\n A&B") == (
        "https://publisher.example/rr/fixture.html"
        "#:~:text=Crisil%20AA%2D%2FStable%2C%20A%26B")
    ranged = evidence.text_fragment(url, "one two three four five six seven eight nine ten")
    assert ranged.endswith("#:~:text=one%20two%20three%20four,seven%20eight%20nine%20ten")


def test_only_a_reproducible_pdf_is_ever_served():
    assert evidence.may_serve_source("application/pdf", "ICRA")
    assert not evidence.may_serve_source("application/pdf", "CRISIL")
    assert not evidence.may_serve_source("text/html", "ICRA")
    assert not evidence.may_serve_source("text/html", "CRISIL")


def test_other_publishers_keep_the_page_image():
    row = {"source_name": "ICRA", "url": "u", "value_text": "x" * 500,
           "anchors": [{"evidence_text": "y" * 500, "bbox": [1], "bbox_rects": [[1]]}]}
    shown = evidence.present_claim(row)
    assert shown["evidence_policy"] == "page_image"
    assert len(shown["anchors"][0]["evidence_text"]) == 500 and shown["anchors"][0]["bbox"]


@pytest.fixture
def committed_crisil_claim():
    """A committed CRISIL HTML claim, for routes that read on their own
    connection. Deleted afterwards (the issuer cascades)."""
    with connect() as c:
        box = make_document(c, "CRISIL")
        paragraph = "Crisil Ratings has reaffirmed its ratings " * 12
        long_html = HTML.replace(b"<p>Liquidity:", b"<p>" + paragraph.encode() + b"</p><p>Liquidity:")
        c.execute("UPDATE document_blob SET bytes = %s WHERE document_id = %s",
                  (long_html, box["document_id"]))
        c.execute("UPDATE document SET sha256 = %s WHERE id = %s",
                  (sha256(long_html).hexdigest(), box["document_id"]))
        t = htmldoc.parse(long_html, CONTENT_TYPE)
        unit = next(u for u in htmldoc.units(t) if u["text"].startswith("Crisil Ratings has"))
        whole = anchor_in(unit, 0, len(unit["text"]))
        claim = ExtractedClaim(claim_type="rationale_point", fact_key="fixture|para",
                               subject="fixture paragraph", value_text=unit["text"],
                               extractor="fixture", extractor_version="1.0.0", anchors=[whole])
        [claim_id] = load_claims(c, box["issuer_id"], box["document_id"], [claim])
        c.commit()
    try:
        yield {**box, "claim_id": claim_id, "full": unit["text"]}
    finally:
        with connect() as c:
            c.execute("DELETE FROM issuer WHERE id = %s", (box["issuer_id"],))
            c.commit()


def test_the_api_serves_a_capped_quote_and_a_link_never_a_copy(committed_crisil_claim):
    box = committed_crisil_claim
    shown = asgi.get(f"/api/claim/{box['claim_id']}").json()
    assert shown["evidence_policy"] == "quote_and_link"
    quote = shown["anchors"][0]
    assert quote["truncated"] and len(quote["evidence_text"]) <= evidence.QUOTE_CAP + 1
    assert quote["evidence_text"] != box["full"] and quote["bbox"] is None
    assert len(shown["value_text"]) <= evidence.QUOTE_CAP + 1
    assert shown["source_link"].startswith("https://publisher.example/rr/fixture.html#:~:text=")
    assert (quote["kind"], quote["node_path"]) == ("html", "/html/body/p[2]")
    for route in (f"/api/pdf/{box['document_id']}",
                  f"/api/page.png?document_id={box['document_id']}&page_no=1"):
        assert asgi.get(route).status == 404, route


def test_the_demo_export_accepts_only_the_allowed_excerpt(committed_crisil_claim):
    from scripts import export_demo

    box = committed_crisil_claim
    claim = export_demo.export_claim(box["claim_id"])
    snapshot = {"claims": {str(box["claim_id"]): claim}, "pages": {}}
    assert not export_demo.rendered(claim, claim["anchors"][0])   # no page image
    export_demo.verify(snapshot)
    claim["anchors"][0]["evidence_text"] = " ".join(box["full"].split())   # the whole thing
    with pytest.raises(AssertionError, match="does not support"):
        export_demo.verify(snapshot)
