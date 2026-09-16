"""Persist ExtractedClaims with verified evidence.

Four integrity rules, all enforced here rather than trusted:
  1. every claim has >= 1 anchor (Pydantic guarantees the shape, this checks DB state)
  2. the offsets are in range: 0 <= char_start < char_end <= len(page.text)
  3. anchor.evidence_text == page.text[char_start:char_end], exactly
  4. the offsets resolve to at least one word rectangle, so the anchor can be drawn

Rule 2 exists because Python slicing does not enforce it: page.text[99999:99999]
on a 4,000-character page is "", and a negative char_start silently slices from
the end. Comparing slices alone therefore passes on offsets that point nowhere.

The whole load runs in one transaction, so a violation rolls the document back
rather than leaving half its claims on disk — the exact case these rules exist
to catch.
"""
from __future__ import annotations

from .ingest import psycopg_json, rects_for_range
from .models import ExtractedClaim, normalize_value


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


def _check_anchor(claim: ExtractedClaim, anchor, page: dict, document_id: int):
    """Validate one anchor against its page. Returns (bbox, rects)."""
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

    # Claims are a pure function of (document, extractor version), so a reload
    # replaces them wholesale. A unique index would be the alternative, but no
    # honest key exists: ICRA's instrument annexure legitimately lists the same
    # ISIN twice with identical date and amount (two tranches of one issue), and
    # a unique constraint would reject that real data.
    conn.execute("DELETE FROM claim WHERE document_id = %s", (document_id,))

    for claim in claims:
        row = conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_numeric, value_unit, value_text, normalized_value,
                               basis, as_of_date, extractor, extractor_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (issuer_id, document_id, claim.claim_type, claim.fact_key, claim.subject,
             claim.value_numeric, claim.value_unit, claim.value_text,
             normalize_value(claim.value_text), claim.basis,
             claim.as_of_date, claim.extractor, claim.extractor_version),
        ).fetchone()
        claim_id = row["id"]
        ids.append(claim_id)

        for ordinal, anchor in enumerate(claim.anchors):
            page = page_cache.setdefault(anchor.page_no, _page(conn, document_id, anchor.page_no))
            bbox, rects = _check_anchor(claim, anchor, page, document_id)
            conn.execute(
                """
                INSERT INTO evidence_anchor (claim_id, document_id, page_no, char_start,
                                             char_end, evidence_text, bbox, bbox_rects, ordinal)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, document_id, anchor.page_no, anchor.char_start, anchor.char_end,
                 anchor.evidence_text, psycopg_json(bbox), psycopg_json(rects), ordinal),
            )

        if claim.rating:
            r = claim.rating
            conn.execute(
                """
                INSERT INTO rating_action (claim_id, agency, instrument, rated_amount_cr, rating,
                                           outlook, watch, action, previous_rating, action_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (claim_id, r.agency, r.instrument, r.rated_amount_cr, r.rating, r.outlook,
                 r.watch, r.action, r.previous_rating, r.action_date),
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
