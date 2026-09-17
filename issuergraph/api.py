"""Read-only API over the evidence graph.

Every endpoint that returns a fact returns the claim id needed to open its
evidence. /api/claim/{id} and /api/page.png are the two halves of the
click-through: the first says where the fact came from, the second draws it.

Every fact endpoint is issuer-scoped. Reconciliation has always been scoped
(reconcile() takes an issuer_id); the read layer was not, so a second issuer
would have blended two companies' debt, ratings and changes into one page
without any error. Scoping is `resolve_issuer`: explicit ?issuer_id wins, a
single loaded issuer is assumed, and two or more without a parameter is a 400
rather than a silent pick of the lowest id.
"""
from __future__ import annotations

import io
import pathlib

import pymupdf
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

from .db import one, query
from .reconcile import TOLERANCE_ABS_CR, TOLERANCE_PCT, _excluded

app = FastAPI(title="IssuerGraph", version="0.1.0")
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
RENDER_ZOOM = 2.0


def resolve_issuer(issuer_id: int | None) -> int:
    """The issuer every fact query filters on.

    Defaulting to "the only issuer" keeps the single-issuer slice working
    without a parameter, but the moment a second issuer is loaded an unscoped
    call becomes ambiguous — and answering an ambiguous question with the
    lowest id is exactly the silent cross-contamination this guards against.
    """
    if issuer_id is not None:
        if not one("SELECT id FROM issuer WHERE id = %s", (issuer_id,)):
            raise HTTPException(404, f"no issuer {issuer_id}")
        return issuer_id
    rows = query("SELECT id FROM issuer ORDER BY id")
    if not rows:
        raise HTTPException(404, "no issuer loaded — run python -m issuergraph.pipeline")
    if len(rows) > 1:
        raise HTTPException(
            400,
            f"{len(rows)} issuers loaded — pass ?issuer_id=; see /api/issuers",
        )
    return rows[0]["id"]


@app.get("/api/issuers")
def issuers():
    return query(
        """
        SELECT i.id, i.name, i.cin,
               count(DISTINCT d.id) AS document_count,
               count(c.id) AS claim_count
        FROM issuer i
        LEFT JOIN document d ON d.issuer_id = i.id
        LEFT JOIN claim c ON c.issuer_id = i.id
        GROUP BY i.id, i.name, i.cin
        ORDER BY i.name
        """
    )


@app.get("/api/issuer")
def issuer(issuer_id: int | None = None):
    issuer_id = resolve_issuer(issuer_id)
    row = one("SELECT id, name, aliases, cin FROM issuer WHERE id = %s", (issuer_id,))
    row["documents"] = query(
        """
        SELECT id, doc_type, source_name, title, url, sha256, byte_size, page_count,
               retrieved_at, published_date, extraction_status, extraction_expected,
               extraction_found, extraction_missing, extracted_at
        FROM document WHERE issuer_id = %s
        ORDER BY published_date NULLS FIRST, id
        """,
        (issuer_id,),
    )
    counts = one("SELECT count(*) AS claims FROM claim WHERE issuer_id = %s", (issuer_id,))
    row["claim_count"] = counts["claims"]
    return row


@app.get("/api/debt")
def debt(issuer_id: int | None = None):
    issuer_id = resolve_issuer(issuer_id)
    totals = query(
        """
        SELECT c.id AS claim_id, c.fact_key, c.basis, c.as_of_date, c.value_numeric,
               c.value_unit, c.subject, d.source_name, d.title, d.published_date
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.claim_type = 'total_borrowings' AND c.issuer_id = %s
        ORDER BY c.basis, c.as_of_date DESC, d.source_name
        """,
        (issuer_id,),
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
          AND c.issuer_id = %s
          AND d.published_date = (SELECT max(published_date) FROM document
                                  WHERE source_name = d.source_name
                                    AND issuer_id = d.issuer_id)
        ORDER BY o.maturity_date, o.amount_cr DESC
        """,
        (issuer_id,),
    )
    return {"totals": totals, "instruments": instruments}


@app.get("/api/ratings")
def ratings(issuer_id: int | None = None):
    issuer_id = resolve_issuer(issuer_id)
    return query(
        """
        SELECT c.id AS claim_id, r.agency, r.instrument, r.instrument_class, r.term,
               r.rated_amount_cr, r.rating, r.outlook, r.watch, r.action, r.action_date,
               s.effective_from, s.effective_to, d.title, d.source_name
        FROM rating_action r
        JOIN claim c ON c.id = r.claim_id
        JOIN document d ON d.id = c.document_id
        LEFT JOIN rating_state s ON s.claim_id = c.id
        WHERE c.fact_key LIKE 'rating_instrument|%%' AND c.issuer_id = %s
        ORDER BY r.action_date DESC, r.agency, r.rated_amount_cr DESC NULLS LAST
        """,
        (issuer_id,),
    )


@app.get("/api/rating-timeline")
def rating_timeline(issuer_id: int | None = None):
    """Each agency's rating of each instrument class, and when it was in force.

    This is the structure conflicts are computed from, exposed so the windows
    behind a disagreement can be read directly rather than inferred.
    """
    return query(
        """
        SELECT instrument_class, term, agency, instrument, grade, outlook, watch,
               effective_from, effective_to, claim_id, withdrawn,
               effective_to IS NULL AS current
        FROM rating_state
        WHERE issuer_id = %s
        ORDER BY instrument_class, term, effective_from DESC, agency
        """,
        (resolve_issuer(issuer_id),),
    )


@app.get("/api/conflicts")
def conflicts(issuer_id: int | None = None, include_resolved: bool = False):
    """Disagreements, open by default.

    reconcile() stamps resolved_at on a conflict that stopped recurring and
    keeps the row, because a disagreement disappearing is itself a signal. That
    history is only useful if the two states are told apart: an open conflict is
    something to act on, a resolved one is something that happened. Returning
    both undifferentiated made every historical conflict read as current, so
    `status` is explicit and non-open rows are excluded unless asked for.

    Three states, because two were not enough: `ended` is a disagreement the
    sources themselves closed (both agencies were on watch until one moved to
    an outlook), which the pipeline will keep re-detecting as a historical
    interval on every run. It is neither current nor "no longer detected".
    """
    rows = query(
        """
        SELECT id, fact_key, subject, kind, tolerance_pct, spread_pct, note,
               first_detected_at, last_seen_at, resolved_at, ended_on,
               CASE WHEN resolved_at IS NOT NULL THEN 'resolved'
                    WHEN ended_on IS NOT NULL THEN 'ended'
                    ELSE 'open' END AS status
        FROM conflict
        WHERE issuer_id = %s AND ((resolved_at IS NULL AND ended_on IS NULL) OR %s)
        ORDER BY resolved_at NULLS FIRST, ended_on NULLS FIRST, kind, fact_key
        """,
        (resolve_issuer(issuer_id), include_resolved),
    )
    for row in rows:
        row["members"] = query(
            """
            SELECT c.id AS claim_id, c.value_numeric, c.value_text, m.stated_value,
                   c.subject, c.basis,
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
def corroborations(issuer_id: int | None = None):
    """Facts two or more independent sources agree on, within both tolerances.

    `variance_cr` is the residual gap. It is usually zero-ish rounding, but it is
    reported rather than hidden: agreement within tolerance is not the same claim
    as an exact match, and the UI must not render them identically.
    """
    rows = query(
        """
        SELECT c.fact_key, min(c.subject) AS subject,
               count(DISTINCT d.source_name) AS sources,
               min(c.value_numeric) AS low, max(c.value_numeric) AS high,
               max(c.value_numeric) - min(c.value_numeric) AS variance_cr,
               (max(c.value_numeric) - min(c.value_numeric))
                   / nullif(min(c.value_numeric), 0) * 100 AS spread_pct,
               json_agg(json_build_object('claim_id', c.id, 'source', d.source_name,
                                          'value', c.value_numeric)
                        ORDER BY d.source_name) AS members
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.value_numeric IS NOT NULL AND c.issuer_id = %s
        GROUP BY c.fact_key
        HAVING count(DISTINCT c.document_id) > 1
           AND count(DISTINCT d.source_name) > 1
           AND (max(c.value_numeric) - min(c.value_numeric))
                   / nullif(min(c.value_numeric), 0) * 100 <= %s
           AND max(c.value_numeric) - min(c.value_numeric) <= %s
        ORDER BY c.fact_key
        """,
        (resolve_issuer(issuer_id), TOLERANCE_PCT, TOLERANCE_ABS_CR),
    )
    # same exclusions reconcile() applies, so the two views cannot drift apart
    return [row for row in rows if not _excluded(row["fact_key"])]


@app.get("/api/changes")
def changes(issuer_id: int | None = None):
    return query(
        """
        SELECT id, agency, from_date, to_date, section, direction, certainty,
               from_claim_id, to_claim_id, from_text, to_text
        FROM rationale_diff
        WHERE issuer_id = %s
        ORDER BY to_date DESC, agency, section, direction
        """,
        (resolve_issuer(issuer_id),),
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


# The public site (landing page, demo, pilot form) registers onto this same app:
# one process, one deployment. The live API above is unchanged.
from . import site  # noqa: E402  (imported here to avoid a circular import)

site.register(app)
