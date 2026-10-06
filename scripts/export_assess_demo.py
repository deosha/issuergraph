"""Export the synthetic Assess cases as a static, read-only public demo.

    .venv/bin/python -m scripts.export_assess_demo [CASE_ID ...]

Writes static/demo/assess/: snapshot.json (each case exactly as the workspace
computes it, plus the evidence for every figure), one PNG per referenced page,
and a sample review pack per case.

Only cases flagged `is_synthetic` can be exported — a real applicant's case is
refused outright, whatever ids are passed. Like the issuer demo export, every
anchor is re-read from the stored page text before it is written; one that
does not re-read fails the export rather than reaching the site.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pymupdf

from issuergraph.assess import analysis, export
from issuergraph.assess.pages import verify
from issuergraph.db import connect

OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "demo" / "assess"
ZOOM = 1.6


class RefusedCase(RuntimeError):
    pass


def synthetic_cases(conn) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assess.case_file WHERE is_synthetic ORDER BY id").fetchall()]


def export_cases(case_ids: list[int], out: pathlib.Path = OUT) -> dict:
    with connect() as conn:
        for cid in case_ids:
            row = conn.execute("SELECT is_synthetic FROM assess.case_file WHERE id=%s", (cid,)).fetchone()
            if not row or not row["is_synthetic"]:
                raise RefusedCase(f"case {cid} is not a synthetic case; only synthetic cases are exported")
    if out.exists():
        shutil.rmtree(out)
    (out / "pages").mkdir(parents=True)
    snap = {"cases": {}, "facts": {}, "txns": {}, "pages": {}, "json": {}}
    with connect() as conn:
        for cid in case_ids:
            built = json.loads(json.dumps(analysis.build(cid), default=str))
            snap["cases"][str(cid)] = built
            _evidence(conn, cid, snap, out)
            export.workbook(analysis.build(cid)).save(out / f"case_{cid}.xlsx")
    text = json.dumps(snap, separators=(",", ":"))
    for marker in ("/Users/", "stored_path", "data/assess"):
        if marker in text:
            raise RuntimeError(f"snapshot would leak a local path ({marker!r})")
    (out / "snapshot.json").write_text(text)
    return {"cases": list(snap["cases"]), "facts": len(snap["facts"]), "txns": len(snap["txns"]),
            "pages": len(snap["pages"])}


def _evidence(conn, cid, snap, out):
    docs = {d["id"]: d for d in conn.execute(
        "SELECT id, kind, filename, media_type, page_count, source_label, stored_path FROM assess.document "
        "WHERE case_id=%s", (cid,)).fetchall()}
    pages = {(p["document_id"], p["page_no"]): p for p in conn.execute(
        "SELECT p.document_id, p.page_no, p.text, p.width, p.height FROM assess.page p "
        "JOIN assess.document d ON d.id=p.document_id WHERE d.case_id=%s", (cid,)).fetchall()}
    needed = set()

    def doc_view(d):
        return {k: d[k] for k in ("id", "kind", "filename", "media_type", "page_count", "source_label")}

    for f in conn.execute("SELECT f.*, i.label AS item_label FROM assess.fact f JOIN assess.item i ON i.id=f.item_id "
                          "WHERE f.case_id=%s", (cid,)).fetchall():
        d = docs[f["document_id"]]
        if f["verified"] and f["spans"]:
            page = pages[(f["document_id"], f["page_no"])]
            verify(page["text"], f["spans"], f["evidence_text"])          # re-read before publishing
            needed.add((f["document_id"], f["page_no"]))
        elif f["verified"] and f["json_path"]:
            raw = json.loads(pathlib.Path(d["stored_path"]).read_bytes())
            parts = f["json_path"].split(".")
            parent = raw
            for p in parts[:-1]:
                parent = parent[int(p)] if isinstance(parent, list) else parent[p]
            snap["json"][f"{d['id']}|{f['json_path']}"] = {"path": f["json_path"], "key": parts[-1], "parent": parent}
        elif f["page_no"] and (f["document_id"], f["page_no"]) in pages:
            needed.add((f["document_id"], f["page_no"]))                  # cited page, shown unhighlighted
        snap["facts"][str(f["id"])] = {
            "id": f["id"], "field": f["field"], "item": f["item_label"], "origin": f["origin"],
            "verified": f["verified"], "page_no": f["page_no"], "evidence_text": f["evidence_text"],
            "json_path": f["json_path"], "note": f["note"], "highlight": bool(f["rects"]),
            "rects": f["rects"] or [], "document": doc_view(d)}
    for t in conn.execute("SELECT id, document_id, page_no, spans, rects, evidence_text FROM assess.txn "
                          "WHERE case_id=%s", (cid,)).fetchall():
        page = pages[(t["document_id"], t["page_no"])]
        verify(page["text"], t["spans"], t["evidence_text"])
        needed.add((t["document_id"], t["page_no"]))
        snap["txns"][str(t["id"])] = {"id": t["id"], "page_no": t["page_no"], "evidence_text": t["evidence_text"],
                                      "verified": True, "origin": "parser", "highlight": True, "rects": t["rects"],
                                      "document": doc_view(docs[t["document_id"]])}
    for doc_id, page_no in sorted(needed):
        d = docs[doc_id]
        pdf = pymupdf.open(d["stored_path"])
        name = f"{doc_id}_{page_no}.png"
        pdf[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM)).save(out / "pages" / name)
        pdf.close()
        p = pages[(doc_id, page_no)]
        snap["pages"][f"{doc_id}|{page_no}"] = {"file": f"pages/{name}", "width": p["width"], "height": p["height"]}


def main():
    with connect() as conn:
        ids = [int(a) for a in sys.argv[1:]] or synthetic_cases(conn)
    print(export_cases(ids))


if __name__ == "__main__":
    main()
