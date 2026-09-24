"""Persist ExtractedClaims with verified evidence.

Four integrity rules, all enforced here rather than trusted:
  1. every claim has >= 1 anchor (Pydantic guarantees the shape, this checks DB state)
  2. the offsets are in range: 0 <= char_start < char_end <= len(text)
  3. anchor.evidence_text == text[char_start:char_end], exactly
  4. the anchor can be shown: a PDF range resolves to at least one word
     rectangle; an HTML node path resolves to exactly one node

For a PDF anchor, `text` is the page's canonical text. For an HTML anchor it is
text_content() of the node at node_path, in a tree re-parsed from the stored
bytes — and only after those bytes re-hash to the document's SHA-256, so a
changed byte fails the document even where no anchor would have noticed.

Rule 2 exists because Python slicing does not enforce it: page.text[99999:99999]
on a 4,000-character page is "", and a negative char_start silently slices from
the end. Comparing slices alone therefore passes on offsets that point nowhere.

The whole load runs in one transaction, so a violation rolls the document back
rather than leaving half its claims on disk — the exact case these rules exist
to catch.
"""
from __future__ import annotations

from hashlib import sha256

from . import htmldoc
from .ingest import psycopg_json, rects_for_range
from .models import ExtractedClaim, normalize_value
from .settings import evidence_policy


class EvidenceMismatch(RuntimeError):
    pass


def _page(conn, document_id: int, page_no: int) -> dict:
    row = conn.execute(
        "SELECT text, word_map FROM document_page WHERE document_id=%s AND page_no=%s",
        (document_id, page_no),
    ).fetchone()
    if row is None:
        raise EvidenceMismatch(f"doc {document_id} has no page {page_no}")
    return row


def _html_tree(conn, document_id: int):
    """The document's stored bytes, re-hashed and re-parsed.

    Returns (tree, source_name). The hash covers the bytes as received, so any
    change to them — even outside every anchored node — fails the document.
    """
    row = conn.execute(
        """
        SELECT d.sha256, d.content_type, d.source_name, b.bytes
        FROM document d LEFT JOIN document_blob b ON b.document_id = d.id
        WHERE d.id = %s
        """,
        (document_id,),
    ).fetchone()
    if row is None or row["bytes"] is None:
        raise EvidenceMismatch(f"doc {document_id} has no stored HTML bytes to verify against")
    data = bytes(row["bytes"])
    if sha256(data).hexdigest() != row["sha256"]:
        raise EvidenceMismatch(
            f"doc {document_id}: stored bytes no longer hash to the document's sha256")
    return htmldoc.parse(data, row["content_type"]), row["source_name"]


def _check_html_anchor(claim: ExtractedClaim, anchor, tree, source_name: str,
                       document_id: int) -> None:
    """Validate one HTML anchor against the node its path names."""
    where = (f"claim {claim.subject!r} doc={document_id} {anchor.node_path} "
             f"[{anchor.char_start}:{anchor.char_end}]")
    node = htmldoc.resolve(tree, anchor.node_path)
    if node is None:
        raise EvidenceMismatch(f"{where}: node path resolves to no single element")
    if node.tag in htmldoc.TABLE_TAGS and evidence_policy(source_name) == "quote_and_link":
        raise EvidenceMismatch(
            f"{where}: a whole-table node cannot be quoted from {source_name}; "
            "anchor the cell or paragraph")
    text = node.text_content()
    if not anchor.evidence_text:
        raise EvidenceMismatch(f"{where}: empty evidence_text proves nothing")
    if anchor.char_start < 0 or anchor.char_end <= anchor.char_start:
        raise EvidenceMismatch(f"{where}: offsets are not a forward range")
    if anchor.char_end > len(text):
        raise EvidenceMismatch(
            f"{where}: range runs past the end of a {len(text)}-character node")
    actual = text[anchor.char_start:anchor.char_end]
    if actual != anchor.evidence_text:
        raise EvidenceMismatch(
            f"{where}\n  anchor says: {anchor.evidence_text!r}\n  node  says: {actual!r}")


def _check_anchor(claim: ExtractedClaim, anchor, page: dict, document_id: int):
    """Validate one PDF anchor against its page. Returns (bbox, rects)."""
    where = (f"claim {claim.subject!r} doc={document_id} p{anchor.page_no} "
             f"[{anchor.char_start}:{anchor.char_end}]")
    text = page["text"]

    if not anchor.evidence_text:
        raise EvidenceMismatch(f"{where}: empty evidence_text proves nothing")
    if anchor.char_start < 0 or anchor.char_end <= anchor.char_start:
        raise EvidenceMismatch(f"{where}: offsets are not a forward range")
    if anchor.char_end > len(text):
        raise EvidenceMismatch(
            f"{where}: range runs past the end of a {len(text)}-character page")

    actual = text[anchor.char_start:anchor.char_end]
    if actual != anchor.evidence_text:
        raise EvidenceMismatch(
            f"{where}\n  anchor says: {anchor.evidence_text!r}\n  page  says: {actual!r}")

    bbox, rects = rects_for_range(page["word_map"], anchor.char_start, anchor.char_end)
    if not rects:
        raise EvidenceMismatch(
            f"{where}: range covers no word boxes, so it would highlight nothing")
    return bbox, rects


def load_claims(conn, issuer_id: int, document_id: int, claims: list[ExtractedClaim]) -> list[int]:
    if conn.autocommit:
        raise EvidenceMismatch(
            "load_claims requires a transactional connection: in autocommit a failed "
            "verification would leave partially-anchored claims on disk")

    with conn.transaction():
        return _load(conn, issuer_id, document_id, claims)


def _load(conn, issuer_id: int, document_id: int, claims: list[ExtractedClaim]) -> list[int]:
    ids: list[int] = []
    page_cache: dict[int, dict] = {}
    html = None                         # (tree, source_name), parsed once per load

    # Claims are a pure function of (document, extractor version), so a reload
    # replaces them wholesale. A unique index would be the alternative, but no
    # honest key exists: ICRA's instrument annexure legitimately lists the same
    # ISIN twice with identical date and amount (two tranches of one issue), and
    # a unique constraint would reject that real data.
    conn.execute("DELETE FROM claim WHERE document_id = %s", (document_id,))
    doc_type = conn.execute("SELECT doc_type FROM document WHERE id = %s",
                            (document_id,)).fetchone()["doc_type"]
    primary = "primary_report" if doc_type == "annual_report" else "primary_rationale"

    for claim in claims:
        row = conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_numeric, value_unit, value_text, normalized_value,
                               basis, as_of_date, extractor, extractor_version, provenance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (issuer_id, document_id, claim.claim_type, claim.fact_key, claim.subject,
             claim.value_numeric, claim.value_unit, claim.value_text,
             normalize_value(claim.value_text), claim.basis,
             claim.as_of_date, claim.extractor, claim.extractor_version,
             claim.provenance or primary),
        ).fetchone()
        claim_id = row["id"]
        ids.append(claim_id)

        for ordinal, anchor in enumerate(claim.anchors):
            if anchor.kind == "html":
                html = html or _html_tree(conn, document_id)
                _check_html_anchor(claim, anchor, *html, document_id)
                bbox = rects = None
            else:
                page = page_cache.setdefault(anchor.page_no,
                                             _page(conn, document_id, anchor.page_no))
                bbox, rects = _check_anchor(claim, anchor, page, document_id)
            conn.execute(
                """
                INSERT INTO evidence_anchor (claim_id, document_id, kind, page_no, node_path,
                                             char_start, char_end, evidence_text, bbox,
                                             bbox_rects, ordinal)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, document_id, anchor.kind, anchor.page_no, anchor.node_path,
                 anchor.char_start, anchor.char_end, anchor.evidence_text,
                 psycopg_json(bbox) if bbox else None,
                 psycopg_json(rects) if rects else None, ordinal),
            )

        if claim.rating:
            r = claim.rating
            conn.execute(
                """
                INSERT INTO rating_action (claim_id, agency, instrument, instrument_class,
                                           term, rated_amount_cr, rating,
                                           outlook, watch, action, previous_rating, action_date,
                                           qualifiers)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, r.agency, r.instrument, r.instrument_class, r.term,
                 r.rated_amount_cr, r.rating, r.outlook,
                 r.watch, r.action, r.previous_rating, r.action_date, r.qualifiers),
            )
        if claim.history:
            h = claim.history
            conn.execute(
                """
                INSERT INTO rating_history_entry (claim_id, agency, instrument,
                                                  instrument_class, term, action_date,
                                                  grade, outlook, watch, withdrawn)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, h.agency, h.instrument, h.instrument_class, h.term,
                 h.action_date, h.grade, h.outlook, h.watch, h.withdrawn),
            )
        if claim.debt:
            d = claim.debt
            conn.execute(
                """
                INSERT INTO debt_observation (claim_id, instrument_name, instrument_type,
                                              amount_cr, maturity_date, secured, as_of_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, d.instrument_name, d.instrument_type, d.amount_cr,
                 d.maturity_date, d.secured, d.as_of_date),
            )

    verify_anchors(conn, document_id)
    return ids


def verify_anchors(conn, document_id: int) -> None:
    orphan = conn.execute(
        """
        SELECT c.id, c.subject FROM claim c
        LEFT JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE c.document_id = %s AND a.id IS NULL
        """,
        (document_id,),
    ).fetchall()
    if orphan:
        raise EvidenceMismatch(f"unanchored claims: {[o['subject'] for o in orphan]}")
