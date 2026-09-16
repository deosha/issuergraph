"""The public demo, served from a committed snapshot.

The demo has no database. That is the point: the deployment that serves the
marketing site holds a curated file and a folder of page images, so a visitor
can click a figure and read the sentence it came from without the pipeline, the
PDFs, or Postgres being anywhere near the internet.

The snapshot is produced by `python -m scripts.export_demo`, which verifies
every anchor against the live page text before writing. What is exported is what
the demo shows and nothing else — these routes cannot reach a record the export
did not curate, because there is nothing else in the file.
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "static" / "demo" / "snapshot.json"


class SnapshotMissing(RuntimeError):
    pass


@lru_cache(maxsize=1)
def snapshot() -> dict:
    if not SNAPSHOT.exists():
        raise SnapshotMissing(
            "demo snapshot not found — run: python -m scripts.export_demo")
    return json.loads(SNAPSHOT.read_text())


def overview() -> dict:
    """Everything the demo renders except the per-claim evidence.

    Counts are computed from the snapshot itself rather than written down, so
    the demo cannot claim a number the data does not contain.
    """
    data = snapshot()
    conflicts = data["conflicts"]
    open_conflicts = [c for c in conflicts if c["status"] == "open"]
    return {
        "sample": True,
        "generated_at": data["generated_at"],
        "document_cutoff": data["document_cutoff"],
        "issuer": data["issuer"],
        "debt": data["debt"],
        "ratings": data["ratings"],
        "timeline": data["timeline"],
        "conflicts": conflicts,
        "corroborations": data["corroborations"],
        "changes": data["changes"],
        "counts": {
            "documents": len(data["issuer"]["documents"]),
            "claims": data["issuer"]["claim_count"],
            "evidenced_claims": len(data["claims"]),
            "conflicts_open": len(open_conflicts),
            "conflicts_resolved": len(conflicts) - len(open_conflicts),
            "corroborations": len(data["corroborations"]),
            "changes": len(data["changes"]),
            "pages": len(data["pages"]),
        },
    }


def claim(claim_id: int) -> dict | None:
    """One claim with its anchors, plus the page image each anchor sits on.

    The anchor keeps its original character offsets and rectangles; the page
    entry carries the page's size in PDF points. The browser positions
    highlights as percentages of that, so the geometry is the pipeline's, not a
    re-derivation, and the image can be rendered at any resolution.
    """
    data = snapshot()
    found = data["claims"].get(str(claim_id))
    if not found:
        return None
    pages = {}
    for anchor in found["anchors"]:
        key = f"{found['document_id']}-{anchor['page_no']}"
        if key in data["pages"]:
            pages[key] = data["pages"][key]
    return {**found, "pages": pages}


def tour() -> list[dict]:
    """The guided tour, built from the snapshot so every step points at real data.

    Each step names the tab to open and, where relevant, the claim or conflict
    to select. Steps whose data is absent are dropped rather than faked — if a
    future snapshot has no resolved differences, the tour simply does not claim
    to show one.
    """
    data = snapshot()
    steps: list[dict] = []

    totals = data["debt"]["totals"]
    anchored = next((t for t in totals if t.get("claim_id")), None)
    if anchored:
        steps.append({
            "tab": "debt", "claim_id": anchored["claim_id"],
            "title": "Start with a figure",
            "body": ("Total borrowings is not a line in an Ind AS balance sheet — it is "
                     "three rows added together. Every figure here is a link."),
        })
        steps.append({
            "tab": "debt", "claim_id": anchored["claim_id"], "open_evidence": True,
            "title": "Open the evidence",
            "body": ("The panel shows the page this came from, with the exact cells "
                     "highlighted: the three components and the statement's own "
                     "'(₹ in Crores)' header, which is what establishes the scale."),
        })

    numeric = next((c for c in data["conflicts"]
                    if c["kind"] == "numeric_disagreement" and c["status"] == "open"), None)
    if numeric:
        steps.append({
            "tab": "conflicts", "conflict_id": numeric["id"],
            "title": "Where sources differ",
            "body": ("A rating agency and the annual report state different total debt "
                     "for the same date. IssuerGraph shows both with their evidence and "
                     "does not choose between them."),
        })

    rating = next((c for c in data["conflicts"]
                   if c["kind"] == "categorical_disagreement" and c["status"] == "open"), None)
    if rating:
        steps.append({
            "tab": "conflicts", "conflict_id": rating["id"],
            "title": "Differences are dated and like-for-like",
            "body": ("Each rating difference names one instrument class, one rating "
                     "scale, one pair of agencies, and the period during which both "
                     "views were in force."),
        })

    if data["ratings"]:
        steps.append({
            "tab": "ratings",
            "title": "The rating timeline",
            "body": ("A rating stands from its action date until the same agency acts "
                     "again on the same instrument class. Agencies are compared only "
                     "where those periods overlap."),
        })

    if data["changes"]:
        steps.append({
            "tab": "changes",
            "title": "What changed between reports",
            "body": ("Successive rationales from one agency, diffed structurally — "
                     "what appeared, what went, what moved. Both sides stay anchored "
                     "to their own document."),
        })

    steps.append({
        "tab": "sources",
        "title": "Every document, with its coverage",
        "body": ("Six public documents, each with its SHA-256, source URL and "
                 "retrieval time — and whether extraction found everything that "
                 "document was expected to yield."),
        "final": True,
    })
    return steps
