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

*It does not show a difference that a missing document could change.* A
difference flagged material_gap was computed over a period in which an agency
took an action we hold only from its rating-history annexure, and that action
changed its grade, outlook or watch. The export refuses such a snapshot unless
--allow-gaps is passed, and records the override in the snapshot when it is.
A gap that is only a reaffirmation leaves every in-force view as stated; it is
shown on the Sources tab and does not block.

*It says when a quoted page has moved on.* A quote_and_link publisher's page
is the reader's only way to see the quote in context, and such pages are
edited in place. Before exporting, each is re-fetched and hashed: a mismatch
marks the document "changed at source since retrieval" (the stored copy stays
the evidence of record) and is listed in the build output. --offline skips it,
and says so.

*No panel says "unknown".* The evidence panel's lines are built by the server;
the export refuses a snapshot where one contains a placeholder word.

The snapshot is committed, so `git diff` after a re-export shows exactly what
changed about the demo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from issuergraph import api, evidence, htmldoc                 # noqa: E402
from issuergraph.db import connect, one, query                 # noqa: E402
from issuergraph.loader import _html_tree                      # noqa: E402
from issuergraph.pipeline import UA                            # noqa: E402

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


class IncompleteCorpus(SystemExit):
    """A conflict the demo would show rests on a document we do not hold."""


def gap_gate(conflicts: list[dict], allow_gaps: bool) -> list[dict]:
    """The conflicts that block an export, or [] when the export may proceed.

    Checked before anything is written, so a refused export leaves the
    committed snapshot and page images exactly as they were.
    """
    flagged = [c for c in conflicts if c.get("material_gap")]
    return [] if allow_gaps else flagged


class PlaceholderInPanel(SystemExit):
    """An evidence panel would show "unknown", "null" or the like."""


def panel_guard(claims: dict[str, dict]) -> list[str]:
    """Every panel string a reader sees that contains a placeholder word."""
    bad = []
    for claim_id, claim in claims.items():
        shown = [*claim.get("panel_lines", []), claim.get("policy_note") or "",
                 claim.get("subject") or "", claim.get("title") or ""]
        for text in shown:
            word = evidence.placeholder_in(text)
            if word:
                bad.append(f"claim {claim_id}: {word!r} in {text!r}")
    return bad


def fetch_sha256(url: str) -> str:
    """The SHA-256 of the body the URL serves now, fetched as the pipeline does."""
    body = subprocess.run(["curl", "-sSL", "--fail", "--max-time", "60", "-A", UA, url],
                          check=True, capture_output=True).stdout
    return hashlib.sha256(body).hexdigest()


def check_sources(fetch=fetch_sha256) -> list[str]:
    """Re-fetch every quote_and_link document; record and report drift.

    A mismatch stamps source_changed_at (first noticed) and the new hash; a
    page that hashes back to what we hold clears both. A page that cannot be
    fetched is reported, and its recorded state is left as it was.
    """
    report = []
    docs = query("SELECT id, source_name, title, url, sha256, source_changed_at, "
                 "source_changed_sha256 FROM document ORDER BY id")
    with connect() as conn:
        for d in docs:
            if not evidence.quote_only(d["source_name"]):
                continue
            try:
                now = fetch(d["url"])
            except Exception as exc:            # noqa: BLE001 - reported, not swallowed
                report.append(f"could not re-fetch #{d['id']} {d['title']}: {exc}")
                continue
            if now == d["sha256"]:
                if d["source_changed_at"]:
                    conn.execute("UPDATE document SET source_changed_at = NULL, "
                                 "source_changed_sha256 = NULL WHERE id = %s", (d["id"],))
                    report.append(f"#{d['id']} {d['title']}: matches the stored copy again")
                continue
            conn.execute(
                "UPDATE document SET source_changed_at = coalesce(source_changed_at, now()), "
                "source_changed_sha256 = %s WHERE id = %s", (now, d["id"]))
            report.append(f"CHANGED AT SOURCE #{d['id']} {d['title']}: stored "
                          f"{d['sha256'][:12]}…, now {now[:12]}… — the stored copy remains "
                          "the evidence of record")
    return report


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--allow-gaps", action="store_true",
                      help="export even if a shown difference has material_gap = true")
    args.add_argument("--offline", action="store_true",
                      help="skip re-fetching quote_and_link sources for drift")
    opts = args.parse_args(argv)

    if opts.offline:
        print("  sources     NOT re-checked (--offline)")
    else:
        drift = check_sources()
        print(f"  sources     re-checked; {len(drift) or 'no'} "
              f"change{'' if len(drift) == 1 else 's'}")
        for line in drift:
            print(f"    {line}")

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
    gaps = api.corpus_gaps(issuer_id=issuer_id)

    blocking = gap_gate(conflicts, opts.allow_gaps)
    if blocking:
        print("refusing to export: a missing document changes the view these "
              "differences were computed over", file=sys.stderr)
        for c in blocking:
            print(f"  #{c['id']} {c['fact_key']} — {c['subject']}", file=sys.stderr)
        missing = [f"{g['agency']} {g['action_date']} ({g['instrument_class']})"
                   for g in gaps["gaps"] if g["changes_view"]]
        print(f"missing primary documents: {', '.join(missing) or 'none recorded'}",
              file=sys.stderr)
        print("load the missing rationales, or pass --allow-gaps to export anyway",
              file=sys.stderr)
        raise IncompleteCorpus(2)

    views = {"issuer": issuer, "debt": debt, "ratings": ratings, "timeline": timeline,
             "conflicts": conflicts, "corroborations": corroborations, "changes": changes,
             "gaps": gaps}

    claim_ids = sorted(claim_ids_in(views))
    claims = {str(cid): export_claim(cid) for cid in claim_ids}
    placeholders = panel_guard(claims)
    if placeholders:
        print("refusing to export: evidence panels would show a placeholder", file=sys.stderr)
        for line in placeholders:
            print(f"  {line}", file=sys.stderr)
        raise PlaceholderInPanel(3)

    if PAGES.exists():
        shutil.rmtree(PAGES)
    PAGES.mkdir(parents=True, exist_ok=True)

    pages: dict[str, dict] = {}
    for claim in claims.values():
        for anchor in claim["anchors"]:
            if not rendered(claim, anchor):
                continue                # quote_and_link or HTML: no page, ever
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
        "gaps": jsonable(gaps),
        # True only when --allow-gaps overrode a difference a missing document
        # could change.
        "incomplete_corpus_allowed": bool(
            opts.allow_gaps and any(c.get("material_gap") for c in conflicts)),
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


def rendered(claim: dict, anchor: dict) -> bool:
    """Is this anchor shown on a page image? Only a PDF anchor from a
    publisher whose page we may reproduce."""
    return anchor.get("kind", "pdf") == "pdf" and claim.get("evidence_policy") == "page_image"


def verify(snapshot: dict) -> None:
    """The snapshot must be able to prove what it displays.

    Checked against the live database while it is still available, because the
    demo has no way to re-derive any of it later. An HTML anchor is re-resolved
    from the stored bytes, and what the snapshot carries must be exactly the
    excerpt of that text the publisher's policy allows — never more.
    """
    with connect() as conn:
        trees: dict[int, object] = {}
        for claim_id, claim in snapshot["claims"].items():
            assert claim["anchors"], f"claim {claim_id} exported without evidence"
            for anchor in claim["anchors"]:
                if anchor.get("kind") == "html":
                    if claim["document_id"] not in trees:
                        trees[claim["document_id"]] = _html_tree(conn, claim["document_id"])[0]
                    node = htmldoc.resolve(trees[claim["document_id"]], anchor["node_path"])
                    assert node is not None, f"claim {claim_id} anchor names no node"
                    actual = node.text_content()[anchor["char_start"]:anchor["char_end"]]
                    shown = (evidence.excerpt(actual)[0]
                             if claim.get("evidence_policy") == "quote_and_link" else actual)
                    assert anchor["evidence_text"] == shown, (
                        f"claim {claim_id} exports text its source does not support")
                    continue
                if claim.get("evidence_policy") == "quote_and_link":
                    # a quoted PDF span: capped, verified against its page text
                    page = conn.execute(
                        "SELECT text FROM document_page WHERE document_id = %s "
                        "AND page_no = %s", (claim["document_id"], anchor["page_no"]),
                    ).fetchone()
                    actual = page["text"][anchor["char_start"]:anchor["char_end"]]
                    assert anchor["evidence_text"] == evidence.excerpt(actual)[0], (
                        f"claim {claim_id} exports text its source does not support")
                    continue
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
            if not rendered(claim, anchor):
                continue
            key = f"{claim['document_id']}-{anchor['page_no']}"
            assert key in snapshot["pages"], f"no page image for {key}"
            assert (OUT / snapshot["pages"][key]["image"]).exists()


if __name__ == "__main__":
    raise SystemExit(main())
