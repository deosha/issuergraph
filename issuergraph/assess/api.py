"""HTTP routes for Assess: /assess (page) and /api/assess/* (JSON, files).

Served to loopback clients only, and not at all when ISSUERGRAPH_DEMO_ONLY is
set (the site's live-API gate closes /api/assess with every other live route;
the page route checks the same flag). Applicant data never reaches /demo,
/app, the public snapshot or any analytics: the Assess page loads no
analytics script.
"""
from __future__ import annotations

import io
import ipaddress
import json
import pathlib
import tempfile

import pymupdf
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..db import connect, one
from ..settings import demo_only
from . import analysis, export, process
from .provider import provider_status

STATIC = pathlib.Path(__file__).resolve().parents[2] / "static"
MAX_UPLOAD = 25 * 1024 * 1024
RENDER_ZOOM = 2.0


def local_only(request: Request) -> None:
    if demo_only():
        raise HTTPException(404, "not served by this deployment")
    host = request.client.host if request.client else ""
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = host in ("localhost", "testclient")
    if not ok:
        raise HTTPException(403, "Assess is served to this machine only")


router = APIRouter(dependencies=[Depends(local_only)])


# --- page --------------------------------------------------------------------------

@router.get("/assess", include_in_schema=False)
@router.get("/assess/{case_id}", include_in_schema=False)
def page(case_id: int | None = None):
    from ..site import ASSET_VERSION

    html = (STATIC / "assess.html").read_text().replace("{{asset_version}}", ASSET_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# --- cases -------------------------------------------------------------------------

class NewCase(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    applicant_name: str | None = Field(default=None, max_length=200)
    requested_product: str | None = Field(default=None, max_length=200)
    requested_amount: float | None = Field(default=None, ge=0)
    borrower_type: str = "salaried"


@router.get("/api/assess/status")
def status():
    return provider_status()


@router.get("/api/assess/cases")
def cases():
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.label, c.applicant_name, c.borrower_type, c.is_synthetic, c.created_at, "
            "count(d.id) AS documents, max(d.processed_at) AS last_processed "
            "FROM assess.case_file c LEFT JOIN assess.document d ON d.case_id=c.id "
            "GROUP BY c.id ORDER BY c.id DESC").fetchall()
    return [{**r, "created_at": r["created_at"].isoformat(),
             "last_processed": r["last_processed"].isoformat() if r["last_processed"] else None}
            for r in rows]


@router.post("/api/assess/cases")
def new_case(body: NewCase):
    if body.borrower_type not in ("salaried", "proprietorship"):
        raise HTTPException(400, "borrower type must be salaried or proprietorship")
    return {"id": process.create_case(body.label, body.applicant_name, body.borrower_type,
                                      body.requested_product, body.requested_amount)}


@router.get("/api/assess/cases/{case_id}")
def case(case_id: int):
    out = analysis.build(case_id)
    if not out:
        raise HTTPException(404, "no such case")
    return json.loads(json.dumps(out, default=str))


# --- documents ---------------------------------------------------------------------------

@router.post("/api/assess/cases/{case_id}/documents")
async def upload(case_id: int, request: Request, background: BackgroundTasks,
                 filename: str, kind: str | None = None):
    if not one("SELECT id FROM assess.case_file WHERE id=%s", (case_id,)):
        raise HTTPException(404, "no such case")
    if kind is not None and kind not in process.KINDS:
        raise HTTPException(400, "unknown document type")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (25 MB limit)")
    name = pathlib.Path(filename).name
    if not name.lower().endswith((".pdf", ".json")):
        raise HTTPException(415, "only PDF and JSON files are accepted")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / name
        path.write_bytes(data)
        doc_id, new = process.add_document(case_id, path, kind, name)
    if new:
        background.add_task(process.process_document, doc_id)
    return {"id": doc_id, "new": new, "status": "queued" if new else "already received"}


class KindChange(BaseModel):
    kind: str


@router.post("/api/assess/documents/{doc_id}/kind")
def set_kind(doc_id: int, body: KindChange, background: BackgroundTasks):
    if body.kind not in process.KINDS:
        raise HTTPException(400, "unknown document type")
    with connect() as conn:
        conn.execute("UPDATE assess.document SET kind=%s, kind_basis='Set by analyst.', status='received' "
                     "WHERE id=%s", (body.kind, doc_id))
    background.add_task(process.process_document, doc_id, True)
    return {"id": doc_id, "status": "queued"}


@router.post("/api/assess/documents/{doc_id}/process")
def reprocess(doc_id: int, background: BackgroundTasks):
    if not one("SELECT id FROM assess.document WHERE id=%s", (doc_id,)):
        raise HTTPException(404, "no such document")
    background.add_task(process.process_document, doc_id, True)
    return {"id": doc_id, "status": "queued"}


# --- evidence -------------------------------------------------------------------------------

def _doc(doc_id: int) -> dict:
    row = one("SELECT id, case_id, kind, filename, stored_path, media_type, page_count, source_label "
              "FROM assess.document WHERE id=%s", (doc_id,))
    if not row:
        raise HTTPException(404, "no such document")
    return row


@router.get("/api/assess/fact/{fact_id}")
def fact(fact_id: int):
    f = one("SELECT f.*, i.label AS item_label FROM assess.fact f JOIN assess.item i ON i.id=f.item_id "
            "WHERE f.id=%s", (fact_id,))
    if not f:
        raise HTTPException(404, "no such fact")
    d = _doc(f["document_id"])
    return {"id": f["id"], "field": f["field"], "item": f["item_label"], "origin": f["origin"],
            "verified": f["verified"], "page_no": f["page_no"], "evidence_text": f["evidence_text"],
            "json_path": f["json_path"], "note": f["note"], "highlight": bool(f["rects"]),
            "document": {k: d[k] for k in ("id", "kind", "filename", "media_type", "page_count",
                                           "source_label")}}


@router.get("/api/assess/txn/{txn_id}")
def txn(txn_id: int):
    t = one("SELECT id, document_id, page_no, evidence_text, txn_date FROM assess.txn WHERE id=%s",
            (txn_id,))
    if not t:
        raise HTTPException(404, "no such transaction")
    d = _doc(t["document_id"])
    return {"id": t["id"], "page_no": t["page_no"], "evidence_text": t["evidence_text"],
            "verified": True, "origin": "parser", "highlight": True,
            "document": {k: d[k] for k in ("id", "kind", "filename", "media_type", "page_count",
                                           "source_label")}}


@router.get("/api/assess/page.png")
def page_png(document_id: int, page_no: int, fact_id: int | None = None, txn_id: int | None = None):
    d = _doc(document_id)
    if d["media_type"] != "application/pdf":
        raise HTTPException(400, "not a PDF")
    rects = []
    if fact_id is not None:
        r = one("SELECT rects, page_no FROM assess.fact WHERE id=%s AND document_id=%s AND verified",
                (fact_id, document_id))
        if r and r["page_no"] == page_no:
            rects = r["rects"] or []
    if txn_id is not None:
        r = one("SELECT rects, page_no FROM assess.txn WHERE id=%s AND document_id=%s", (txn_id, document_id))
        if r and r["page_no"] == page_no:
            rects = r["rects"] or []
    pdf = pymupdf.open(d["stored_path"])
    try:
        if not 1 <= page_no <= pdf.page_count:
            raise HTTPException(404, "no such page")
        page = pdf[page_no - 1]
        for rect in rects:
            annot = page.add_highlight_annot(pymupdf.Rect(*rect))
            annot.set_colors(stroke=(0.36, 0.82, 0.78))
            annot.update()
        png = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM)).tobytes("png")
    finally:
        pdf.close()
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/assess/json/{doc_id}")
def json_source(doc_id: int, path: str):
    """The JSON object holding `path`, pretty-printed, for the evidence pane."""
    d = _doc(doc_id)
    if d["media_type"] != "application/json":
        raise HTTPException(400, "not JSON")
    obj = json.loads(pathlib.Path(d["stored_path"]).read_bytes())
    parts = path.split(".")
    parent = obj
    for p in parts[:-1]:
        parent = parent[int(p)] if isinstance(parent, list) else parent[p]
    return {"path": path, "key": parts[-1], "parent": parent}


# --- analyst input -----------------------------------------------------------------------------

class Correction(BaseModel):
    target: str
    target_id: str = Field(min_length=1, max_length=300)
    field: str = Field(min_length=1, max_length=60)
    value: dict
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/api/assess/cases/{case_id}/corrections")
def correct(case_id: int, body: Correction):
    if body.target not in ("fact", "txn", "obligation") or not body.reason.strip():
        raise HTTPException(400, "target must be fact, txn or obligation, with a reason")
    original = None
    if body.target == "fact":
        doc, seq, field = body.target_id.split(":", 2)
        row = one("SELECT f.value_num, f.value_text, f.value_date FROM assess.fact f "
                  "JOIN assess.item i ON i.id=f.item_id WHERE f.case_id=%s AND f.document_id=%s "
                  "AND i.seq=%s AND f.field=%s", (case_id, int(doc), int(seq), field))
        if not row:
            raise HTTPException(404, "no such fact")
        original = json.loads(json.dumps(row, default=str))
        v = body.value.get("value")
        if v is not None and not isinstance(v, (int, float, str)):
            raise HTTPException(400, "value must be a number, text or null")
    elif body.target == "txn":
        doc, seq = body.target_id.split(":")
        row = one("SELECT category FROM assess.txn WHERE case_id=%s AND document_id=%s AND seq=%s",
                  (case_id, int(doc), int(seq)))
        if not row:
            raise HTTPException(404, "no such transaction")
        if body.value.get("value") not in ("salary", "loan_repayment", "card_payment", "od_interest",
                                           "other"):
            raise HTTPException(400, "unknown category")
        original = row
    else:
        inc = body.value.get("include")
        amt = body.value.get("amount")
        if not isinstance(inc, bool):
            raise HTTPException(400, "include must be true or false")
        if inc and (not isinstance(amt, (int, float)) or amt < 0):
            raise HTTPException(400, "an included obligation needs a non-negative monthly amount")
        if inc and not str(body.value.get("basis") or "").strip():
            raise HTTPException(400, "an included obligation needs its basis")
    with connect() as conn:
        conn.execute("INSERT INTO assess.correction (case_id, target, target_id, field, original, value, "
                     "reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (case_id, body.target, body.target_id, body.field,
                      json.dumps(original) if original is not None else None, json.dumps(body.value),
                      body.reason.strip()))
    return {"ok": True}


class Assumption(BaseModel):
    key: str
    value: float | None
    basis: str = Field(min_length=1, max_length=1000)


@router.post("/api/assess/cases/{case_id}/assumptions")
def assume(case_id: int, body: Assumption):
    if body.key not in analysis.ASSUMPTION_KEYS:
        raise HTTPException(400, "unknown assumption")
    with connect() as conn:
        conn.execute("INSERT INTO assess.assumption (case_id, key, value, basis) VALUES (%s,%s,%s,%s)",
                     (case_id, body.key, body.value, body.basis.strip()))
    return {"ok": True}


class Decision(BaseModel):
    match_key: str = Field(min_length=1, max_length=500)
    status: str
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/api/assess/cases/{case_id}/decisions")
def decide(case_id: int, body: Decision):
    if body.status not in ("confirmed", "rejected"):
        raise HTTPException(400, "status must be confirmed or rejected")
    with connect() as conn:
        conn.execute("INSERT INTO assess.decision (case_id, match_key, status, reason) VALUES (%s,%s,%s,%s)",
                     (case_id, body.match_key, body.status, body.reason.strip()))
    return {"ok": True}


class Ack(BaseModel):
    item_key: str = Field(min_length=1, max_length=500)
    note: str = Field(min_length=1, max_length=1000)


@router.post("/api/assess/cases/{case_id}/acks")
def ack(case_id: int, body: Ack):
    with connect() as conn:
        conn.execute("INSERT INTO assess.review_ack (case_id, item_key, note) VALUES (%s,%s,%s)",
                     (case_id, body.item_key, body.note.strip()))
    return {"ok": True}


@router.get("/api/assess/cases/{case_id}/export.xlsx")
def export_xlsx(case_id: int):
    out = analysis.build(case_id)
    if not out:
        raise HTTPException(404, "no such case")
    buf = io.BytesIO()
    export.workbook(out).save(buf)
    name = f"assess_case_{case_id}_{out['generated_at']}.xlsx"
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})
