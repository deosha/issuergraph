"""Export the curated demo snapshot: python -m scripts.export_demo

Writes static/demo/snapshot.json and static/demo/pages/*.jpg from the live
database, so the public demo runs with no database and no PDFs — the deployment
that serves the marketing site does not need the pipeline at all.

Two properties matter and are asserted here rather than hoped for:

*The evidence is real.* Every claim in the snapshot carries its own anchors with
their original character offsets and rectangles, and the page images are the
actual pages those rectangles index into. The demo highlights by geometry, the
same way the live product does; nothing is re-searched or approximated.

*Only what the demo shows is exported.* Claims are collected by walking the
views the demo actually renders, so the snapshot is a curated subset rather than
a database dump served from a public route.

The snapshot is committed, so `git diff` after a re-export shows exactly what
changed about the demo.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
from datetime import date, datetime
from decimal import Decimal

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from issuergraph import api                                    # noqa: E402
from issuergraph.db import connect, one, query                 # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "demo"
PAGES = OUT / "pages"
ZOOM = 1.6              # legible on a laptop; the overlay scales, so this is free
JPEG_QUALITY = 72


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def claim_ids_in(payload) -> set[int]:
    """Every claim id reachable from a rendered view."""
    found: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("claim_id", "from_claim_id", "to_claim_id") and isinstance(value, int):
                found.add(value)
            else:
                found |= claim_ids_in(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= claim_ids_in(item)
    return found


def export_claim(claim_id: int) -> dict:
    claim = api.claim(claim_id)
    return jsonable(claim)


def render_page(document_id: int, page_no: int) -> dict:
    row = one("SELECT local_path FROM document WHERE id = %s", (document_id,))
    page_row = one(
        "SELECT width, height FROM document_page WHERE document_id = %s AND page_no = %s",
        (document_id, page_no),
    )
    pdf = pymupdf.open(row["local_path"])
    pixmap = pdf[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
    name = f"{document_id}-{page_no}.jpg"
    (PAGES / name).write_bytes(pixmap.tobytes("jpg", jpg_quality=JPEG_QUALITY))
    pdf.close()
    # width/height are PDF points: anchor rectangles live in that space, and the
    # demo positions highlights as percentages of it, so the image can be any size.
    return {"image": f"pages/{name}", "width": float(page_row["width"]),
            "height": float(page_row["height"])}


# Internal bookkeeping the demo never displays. Exporting it put a timestamp
# diff in every re-export — a test run re-extracts a document on purpose — which
# buries the changes worth reviewing. retrieved_at stays: the demo shows it.
UNDISPLAYED = ("extracted_at",)


def main() -> int:
    issuer = api.issuer()
    for document in issuer["documents"]:
        for field in UNDISPLAYED:
            document.pop(field, None)
    issuer_id = issuer["id"]
    debt = api.debt(issuer_id=issuer_id)
    ratings = api.ratings(issuer_id=issuer_id)
    timeline = api.rating_timeline(issuer_id=issuer_id)
    conflicts = api.conflicts(issuer_id=issuer_id, include_resolved=True)
    corroborations = api.corroborations(issuer_id=issuer_id)
    changes = api.changes(issuer_id=issuer_id)

    views = {"issuer": issuer, "debt": debt, "ratings": ratings, "timeline": timeline,
             "conflicts": conflicts, "corroborations": corroborations, "changes": changes}

    claim_ids = sorted(claim_ids_in(views))
    claims = {str(cid): export_claim(cid) for cid in claim_ids}

    if PAGES.exists():
        shutil.rmtree(PAGES)
    PAGES.mkdir(parents=True, exist_ok=True)

    pages: dict[str, dict] = {}
    for claim in claims.values():
        for anchor in claim["anchors"]:
            key = f"{claim['document_id']}-{anchor['page_no']}"
            if key not in pages:
                pages[key] = render_page(claim["document_id"], anchor["page_no"])

    published = [d["published_date"] for d in issuer["documents"] if d["published_date"]]
    cutoff = max(published) if published else None

    snapshot = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # The demo states this date on screen: a sample dataset that does not say
        # how old it is invites someone to read it as current.
        "document_cutoff": jsonable(cutoff),
        "sample": True,
        "issuer": jsonable(issuer),
        "debt": jsonable(debt),
        "ratings": jsonable(ratings),
        "timeline": jsonable(timeline),
        "conflicts": jsonable(conflicts),
        "corroborations": jsonable(corroborations),
        "changes": jsonable(changes),
        "claims": claims,
        "pages": pages,
    }

    verify(snapshot)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "snapshot.json").write_text(json.dumps(snapshot, indent=1, sort_keys=False))

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"  claims      {len(claims)}")
    print(f"  pages       {len(pages)} rendered at {ZOOM}x")
    print(f"  conflicts   {len(conflicts)} "
          f"({sum(1 for c in conflicts if c['status'] == 'open')} open)")
    print(f"  changes     {len(changes)}")
    print(f"  cutoff      {snapshot['document_cutoff']}")
    print(f"  size        {size / 1e6:.1f} MB  ->  {OUT}")
    return 0


def verify(snapshot: dict) -> None:
    """The snapshot must be able to prove what it displays.

    Checked against the live database while it is still available, because the
    demo has no way to re-derive any of it later.
    """
    with connect() as conn:
        for claim_id, claim in snapshot["claims"].items():
            assert claim["anchors"], f"claim {claim_id} exported without evidence"
            for anchor in claim["anchors"]:
                page = conn.execute(
                    "SELECT text FROM document_page WHERE document_id = %s AND page_no = %s",
                    (claim["document_id"], anchor["page_no"]),
                ).fetchone()
                actual = page["text"][anchor["char_start"]:anchor["char_end"]]
                assert actual == anchor["evidence_text"], (
                    f"claim {claim_id} anchor drifted from its page")
                assert anchor["bbox_rects"], f"claim {claim_id} anchor has no geometry"

    for claim in snapshot["claims"].values():
        for anchor in claim["anchors"]:
            key = f"{claim['document_id']}-{anchor['page_no']}"
            assert key in snapshot["pages"], f"no page image for {key}"
            assert (OUT / snapshot["pages"][key]["image"]).exists()


if __name__ == "__main__":
    raise SystemExit(main())
