"""Persist ExtractedClaims with verified evidence.

Two integrity rules, both enforced here rather than trusted:
  1. every claim has >= 1 anchor (Pydantic guarantees the shape, this checks DB state)
  2. anchor.evidence_text == page.text[char_start:char_end], exactly
A violation raises and rolls back the ingest run.
"""
from __future__ import annotations

from .ingest import psycopg_json, rects_for_range
from .models import ExtractedClaim


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


def load_claims(conn, issuer_id: int, document_id: int, claims: list[ExtractedClaim]) -> list[int]:
    ids: list[int] = []
    page_cache: dict[int, dict] = {}

    for claim in claims:
        row = conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_numeric, value_unit, value_text, basis, as_of_date,
                               extractor, extractor_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (issuer_id, document_id, claim.claim_type, claim.fact_key, claim.subject,
             claim.value_numeric, claim.value_unit, claim.value_text, claim.basis,
             claim.as_of_date, claim.extractor, claim.extractor_version),
        ).fetchone()
        claim_id = row["id"]
        ids.append(claim_id)

        for ordinal, anchor in enumerate(claim.anchors):
            page = page_cache.setdefault(anchor.page_no, _page(conn, document_id, anchor.page_no))
            actual = page["text"][anchor.char_start:anchor.char_end]
            if actual != anchor.evidence_text:
                raise EvidenceMismatch(
                    f"claim '{claim.subject}' doc={document_id} p{anchor.page_no} "
                    f"[{anchor.char_start}:{anchor.char_end}]\n"
                    f"  anchor says: {anchor.evidence_text!r}\n"
                    f"  page  says: {actual!r}"
                )
            bbox, rects = rects_for_range(page["word_map"], anchor.char_start, anchor.char_end)
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
