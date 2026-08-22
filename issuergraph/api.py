"""Read-only API over the evidence graph.

Every endpoint that returns a fact returns the claim id needed to open its
evidence. /api/claim/{id} and /api/page.png are the two halves of the
click-through: the first says where the fact came from, the second draws it.
"""
from __future__ import annotations

import io
import pathlib

import pymupdf
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import one, query

app = FastAPI(title="IssuerGraph", version="0.1.0")
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
RENDER_ZOOM = 2.0


@app.get("/api/issuer")
def issuer():
    row = one("SELECT id, name, aliases, cin FROM issuer ORDER BY id LIMIT 1")
    if not row:
        raise HTTPException(404, "no issuer loaded — run python -m issuergraph.pipeline")
    row["documents"] = query(
        """
        SELECT id, doc_type, source_name, title, url, sha256, byte_size, page_count,
               retrieved_at, published_date
        FROM document WHERE issuer_id = %s
        ORDER BY published_date NULLS FIRST, id
        """,
        (row["id"],),
    )
    counts = one("SELECT count(*) AS claims FROM claim WHERE issuer_id = %s", (row["id"],))
    row["claim_count"] = counts["claims"]
    return row


@app.get("/api/debt")
def debt():
    totals = query(
        """
        SELECT c.id AS claim_id, c.fact_key, c.basis, c.as_of_date, c.value_numeric,
               c.subject, d.source_name, d.title, d.published_date
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.claim_type = 'total_borrowings'
        ORDER BY c.basis, c.as_of_date DESC, d.source_name
        """
    )
    for row in totals:
        row["conflict"] = one(
            """
            SELECT k.id, k.kind, k.spread_pct, k.note FROM conflict k
            JOIN conflict_member m ON m.conflict_id = k.id
            WHERE m.claim_id = %s
            """,
            (row["claim_id"],),
        )
    instruments = query(
        """
        SELECT c.id AS claim_id, o.instrument_name, o.instrument_type, o.amount_cr,
               o.maturity_date, o.as_of_date, d.source_name, d.title
        FROM debt_observation o
        JOIN claim c ON c.id = o.claim_id
        JOIN document d ON d.id = c.document_id
        WHERE o.instrument_type <> 'total' AND o.maturity_date IS NOT NULL
          AND d.published_date = (SELECT max(published_date) FROM document
                                  WHERE source_name = d.source_name)
        ORDER BY o.maturity_date, o.amount_cr DESC
        """
    )
    return {"totals": totals, "instruments": instruments}


@app.get("/api/ratings")
def ratings():
    return query(
        """
        SELECT c.id AS claim_id, r.agency, r.instrument, r.rated_amount_cr, r.rating,
               r.outlook, r.watch, r.action, r.action_date, d.title, d.source_name
        FROM rating_action r
        JOIN claim c ON c.id = r.claim_id
        JOIN document d ON d.id = c.document_id
        WHERE c.fact_key LIKE 'rating_instrument|%%'
        ORDER BY r.action_date DESC, r.agency, r.rated_amount_cr DESC NULLS LAST
        """
    )


@app.get("/api/conflicts")
def conflicts():
    rows = query(
        "SELECT id, fact_key, subject, kind, tolerance_pct, spread_pct, note "
        "FROM conflict ORDER BY kind, fact_key"
    )
    for row in rows:
        row["members"] = query(
            """
            SELECT c.id AS claim_id, c.value_numeric, c.value_text, c.subject, c.basis,
                   c.as_of_date, d.source_name, d.title, d.published_date
            FROM conflict_member m
            JOIN claim c ON c.id = m.claim_id
            JOIN document d ON d.id = c.document_id
            WHERE m.conflict_id = %s
            ORDER BY d.published_date NULLS LAST, d.source_name
            """,
            (row["id"],),
        )
    return rows


@app.get("/api/corroborations")
def corroborations():
    """Facts that two or more independent documents agree on."""
    return query(
        """
        SELECT c.fact_key, min(c.subject) AS subject, count(DISTINCT c.document_id) AS sources,
               min(c.value_numeric) AS low, max(c.value_numeric) AS high,
               json_agg(json_build_object('claim_id', c.id, 'source', d.source_name,
                                          'value', c.value_numeric)
                        ORDER BY d.source_name) AS members
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.claim_type = 'total_borrowings'
        GROUP BY c.fact_key
        HAVING count(DISTINCT c.document_id) > 1
           AND (max(c.value_numeric) - min(c.value_numeric)) / min(c.value_numeric) * 100 <= 0.10
        ORDER BY c.fact_key
        """
    )


@app.get("/api/changes")
def changes():
    return query(
        """
        SELECT id, agency, from_date, to_date, section, direction,
               from_claim_id, to_claim_id, from_text, to_text
        FROM rationale_diff
        ORDER BY to_date DESC, agency, section, direction
        """
    )


@app.get("/api/claim/{claim_id}")
def claim(claim_id: int):
    row = one(
        """
        SELECT c.id, c.claim_type, c.fact_key, c.subject, c.value_numeric, c.value_unit,
               c.value_text, c.basis, c.as_of_date, c.extractor, c.extractor_version,
               d.id AS document_id, d.title, d.source_name, d.url, d.sha256,
               d.retrieved_at, d.published_date, d.page_count
        FROM claim c JOIN document d ON d.id = c.document_id WHERE c.id = %s
        """,
        (claim_id,),
    )
    if not row:
        raise HTTPException(404, "no such claim")
    row["anchors"] = query(
        """
        SELECT page_no, char_start, char_end, evidence_text, bbox, bbox_rects, ordinal
        FROM evidence_anchor WHERE claim_id = %s ORDER BY ordinal
        """,
        (claim_id,),
    )
    return row


@app.get("/api/page.png")
def page_png(document_id: int, page_no: int, claim_id: int | None = None):
    doc_row = one("SELECT local_path FROM document WHERE id = %s", (document_id,))
    if not doc_row:
        raise HTTPException(404, "no such document")

    rects = []
    if claim_id is not None:
        for anchor in query(
            "SELECT bbox_rects FROM evidence_anchor WHERE claim_id = %s AND page_no = %s",
            (claim_id, page_no),
        ):
            rects.extend(anchor["bbox_rects"] or [])

    pdf = pymupdf.open(doc_row["local_path"])
    page = pdf[page_no - 1]
    for rect in rects:
        page.add_highlight_annot(pymupdf.Rect(*rect))
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
    buffer = io.BytesIO(pixmap.tobytes("png"))
    pdf.close()
    return Response(buffer.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/pdf/{document_id}")
def pdf(document_id: int):
    row = one("SELECT local_path, title FROM document WHERE id = %s", (document_id,))
    if not row:
        raise HTTPException(404, "no such document")
    return FileResponse(row["local_path"], media_type="application/pdf")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
