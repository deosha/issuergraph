"""The pipeline: fetch → ingest → extract → load → reconcile → diff.

An ordered function. Each stage's output is the next stage's input; failures
abort the transaction rather than degrade silently.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
from datetime import datetime, timezone

from .completeness import detect_gaps
from .corpus import ISSUER, SOURCES
from .db import connect
from .diff import build_diffs
from .extractors import coverage as coverage_check
from . import htmldoc
from .ingest import RAW_DIR, ingest_document, ingest_html_document
from .loader import load_claims
from .reconcile import reconcile

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")


def fetch_missing() -> list[str]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    fetched = []
    for source in SOURCES:
        path = RAW_DIR / source.filename
        if path.exists() and path.stat().st_size > 0:
            continue
        # No --compressed: the stored bytes are the body exactly as served,
        # which is what the SHA-256 covers. An HTML page's charset may be
        # declared only in its Content-Type, so its headers are kept too.
        headers = ["-D", str(headers_path(path))] if source.media_type == "text/html" else []
        subprocess.run(["curl", "-sSL", "--fail", "--max-time", "180", "-A", UA, *headers,
                        "-o", str(path), source.url], check=True)
        fetched.append(source.filename)
    return fetched


def headers_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".headers")


def content_type_of(path: pathlib.Path) -> str | None:
    """The last Content-Type in a saved header dump (after any redirects)."""
    hp = headers_path(path)
    if not hp.exists():
        return None
    found = None
    for line in hp.read_text(errors="replace").splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-type":
            found = value.strip()
    return found


def ensure_issuer(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO issuer (name, aliases, cin) VALUES (%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET aliases = EXCLUDED.aliases
        RETURNING id
        """,
        (ISSUER["name"], ISSUER["aliases"], ISSUER["cin"]),
    ).fetchone()
    return row["id"]


def pages_of(conn, document_id: int) -> list[dict]:
    """The units an extractor reads: a PDF's pages, or an HTML page's nodes
    parsed from its stored bytes — the same bytes the loader verifies against."""
    doc = conn.execute(
        """
        SELECT d.media_type, d.content_type, b.bytes
        FROM document d LEFT JOIN document_blob b ON b.document_id = d.id WHERE d.id = %s
        """,
        (document_id,),
    ).fetchone()
    if doc and doc["media_type"] == "text/html":
        return htmldoc.units(htmldoc.parse(bytes(doc["bytes"]), doc["content_type"]))
    return conn.execute(
        "SELECT page_no, text, word_map FROM document_page "
        "WHERE document_id = %s ORDER BY page_no",
        (document_id,),
    ).fetchall()


class IncompleteExtraction(RuntimeError):
    """Raised in strict mode when a document does not meet its own declaration."""


class ConflictEvidenceLost(RuntimeError):
    """Re-extraction would leave a recorded conflict without its evidence."""


def conflict_members_of(conn, document_id: int) -> list[dict]:
    """The conflict membership this document's claims hold, by what they state.

    Re-extraction replaces a document's claims and conflict_member cascades on
    claim deletion. An open conflict is re-recorded by reconcile in the same
    run, but a resolved one is not, so without carrying membership across a
    resolved conflict silently lost this document's side and read as one
    source disagreeing with nobody.
    """
    return conn.execute(
        """
        SELECT m.conflict_id, m.stated_value, c.fact_key, c.value_text, c.value_numeric
        FROM conflict_member m JOIN claim c ON c.id = m.claim_id
        WHERE c.document_id = %s
        """,
        (document_id,),
    ).fetchall()


def relink_conflict_members(conn, document_id: int, members: list[dict]) -> None:
    """Point carried membership at the replacement claims stating the same thing.

    Matched on fact key *and* on still saying what the conflict recorded: a
    replacement that states something else is not that evidence, and attaching
    history to it would misreport what the source said. That case aborts the
    run. For a rating the recorded value is the grade, outlook or watch named
    in stated_value — not the claim's display text, which an extractor fix may
    legitimately trim (CARE 2.2.0 stopped folding the action wording into it).
    """
    lost = []
    for m in members:
        replacements = conn.execute(
            """
            SELECT c.id FROM claim c
            LEFT JOIN rating_action r ON r.claim_id = c.id
            LEFT JOIN rating_history_entry h ON h.claim_id = c.id
            WHERE c.document_id = %(doc)s AND c.fact_key = %(key)s
              AND CASE WHEN r.claim_id IS NOT NULL AND %(stated)s::text IS NOT NULL
                       THEN %(stated)s::text IN (r.rating, r.outlook, r.watch)
                       WHEN h.claim_id IS NOT NULL AND %(stated)s::text IS NOT NULL
                       THEN %(stated)s::text IN (h.grade, h.outlook, h.watch)
                       ELSE c.value_text IS NOT DISTINCT FROM %(text)s
                        AND c.value_numeric IS NOT DISTINCT FROM %(num)s
                  END
            """,
            {"doc": document_id, "key": m["fact_key"], "stated": m["stated_value"],
             "text": m["value_text"], "num": m["value_numeric"]},
        ).fetchall()
        if not replacements:
            lost.append(f"conflict {m['conflict_id']}: {m['fact_key']}")
        for r in replacements:
            conn.execute(
                "INSERT INTO conflict_member (conflict_id, claim_id, stated_value) "
                "VALUES (%s,%s,%s) ON CONFLICT (conflict_id, claim_id) DO NOTHING",
                (m["conflict_id"], r["id"], m["stated_value"]),
            )
    if lost:
        raise ConflictEvidenceLost(
            f"document {document_id}: re-extraction no longer states "
            + "; ".join(sorted(set(lost))))


def gap_evidence_of(conn, document_id: int) -> list[dict]:
    """Corpus-gap evidence held by this document's history claims. Open gaps
    are recomputed each run; a resolved one is not, so, as with conflicts, its
    evidence is carried across a re-extraction rather than cascaded away."""
    return conn.execute(
        """
        SELECT e.gap_id, c.fact_key, h.grade, g.resolved_at IS NOT NULL AS resolved
        FROM corpus_gap_evidence e
        JOIN claim c ON c.id = e.claim_id
        JOIN rating_history_entry h ON h.claim_id = c.id
        JOIN corpus_gap g ON g.id = e.gap_id
        WHERE c.document_id = %s
        """,
        (document_id,),
    ).fetchall()


def relink_gap_evidence(conn, document_id: int, evidence: list[dict]) -> None:
    lost = []
    for e in evidence:
        replacements = conn.execute(
            """
            SELECT c.id FROM claim c JOIN rating_history_entry h ON h.claim_id = c.id
            WHERE c.document_id = %s AND c.fact_key = %s
              AND h.grade IS NOT DISTINCT FROM %s
            """,
            (document_id, e["fact_key"], e["grade"]),
        ).fetchall()
        if not replacements and e["resolved"]:
            lost.append(f"gap {e['gap_id']}: {e['fact_key']}")
        for r in replacements:
            conn.execute("INSERT INTO corpus_gap_evidence (gap_id, claim_id) VALUES (%s,%s) "
                         "ON CONFLICT DO NOTHING", (e["gap_id"], r["id"]))
    if lost:
        raise ConflictEvidenceLost(
            f"document {document_id}: re-extraction no longer states "
            + "; ".join(sorted(set(lost))))


def record_coverage(conn, document_id: int, cov) -> None:
    conn.execute(
        """
        UPDATE document
           SET extraction_status = %s, extraction_expected = %s,
               extraction_found = %s, extraction_missing = %s, extracted_at = now()
         WHERE id = %s
        """,
        (cov.status, cov.expected, cov.found, cov.missing, document_id),
    )


def extraction_needed(conn, document_id: int, extractor, force: bool = False):
    """Should this document be extracted, and why? Returns (reason, claims, version).

    A document is not "done" because it has claims — it is done because it has
    claims from the extractor version we currently ship. Comparing the two is
    what makes an extractor fix reach documents that are already ingested; the
    old behaviour left them frozen at whatever parser first read them, so a
    correction silently applied only to issuers loaded after it.
    """
    rows = conn.execute(
        """
        SELECT count(*) AS n,
               array_agg(DISTINCT extractor_version) AS versions
        FROM claim WHERE document_id = %s
        """,
        (document_id,),
    ).fetchone()
    count = rows["n"]
    versions = [v for v in (rows["versions"] or []) if v]
    previous = versions[0] if len(versions) == 1 else (
        ", ".join(sorted(versions)) if versions else None)

    if not count:
        return "initial", 0, None
    if force:
        return "forced", count, previous
    if versions != [extractor.VERSION]:
        return "version_changed", count, previous
    return None, count, previous


def run(reset: bool = False, strict: bool = False, reprocess: bool = False) -> dict:
    fetched = fetch_missing()
    stats = {"fetched": fetched, "documents": [], "claims": 0,
             "incomplete": [], "reprocessed": []}

    with connect() as conn:
        if reset:
            conn.execute("TRUNCATE issuer RESTART IDENTITY CASCADE")
        issuer_id = ensure_issuer(conn)

        for source in SOURCES:
            path = pathlib.Path(RAW_DIR / source.filename)
            retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if source.media_type == "text/html":
                document_id = ingest_html_document(conn, issuer_id, path, source.meta,
                                                   content_type_of(path),
                                                   retrieved_at=retrieved_at)
            else:
                document_id = ingest_document(conn, issuer_id, path, source.meta,
                                              retrieved_at=retrieved_at)

            reason, already, previous = extraction_needed(
                conn, document_id, source.extractor, force=reprocess)
            if reason is None:
                stats["documents"].append({"file": source.filename, "id": document_id,
                                           "claims": already, "status": "cached"})
                stats["claims"] += already
                continue

            pages = pages_of(conn, document_id)
            claims = source.extractor.extract(source.meta, pages)
            cov = coverage_check.check(source.extractor, source.meta, pages, claims)
            record_coverage(conn, document_id, cov)
            if not cov.complete:
                stats["incomplete"].append({"file": source.filename, "id": document_id,
                                            "missing": cov.missing})
                if strict:
                    raise IncompleteExtraction(f"{source.filename}: {cov.summary()}")

            # the document's own stated date, discovered during extraction
            dates = [c.as_of_date for c in claims if c.as_of_date]
            if source.meta.doc_type == "rating_rationale" and dates:
                conn.execute("UPDATE document SET published_date = %s WHERE id = %s",
                             (max(set(dates), key=dates.count), document_id))

            members, gap_evidence = [], []
            if already:
                # Replaced, not appended: two versions of the same fact from one
                # document are not two sources agreeing, they are one parser
                # changing its mind, and reconciliation must never see both.
                members = conflict_members_of(conn, document_id)
                gap_evidence = gap_evidence_of(conn, document_id)
                conn.execute("DELETE FROM claim WHERE document_id = %s", (document_id,))

            load_claims(conn, issuer_id, document_id, claims)
            relink_conflict_members(conn, document_id, members)
            relink_gap_evidence(conn, document_id, gap_evidence)
            conn.execute(
                """
                INSERT INTO extraction_run (document_id, extractor, extractor_version,
                                            reason, previous_version, claims_written,
                                            claims_replaced, coverage_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (document_id, source.extractor.EXTRACTOR, source.extractor.VERSION,
                 reason, previous, len(claims), already, cov.status),
            )
            stats["documents"].append({"file": source.filename, "id": document_id,
                                       "claims": len(claims),
                                       "status": "extracted" if reason == "initial"
                                       else f"re-extracted ({reason})",
                                       "coverage": cov.status})
            if reason != "initial":
                stats["reprocessed"].append(
                    {"file": source.filename, "from": previous,
                     "to": source.extractor.VERSION, "was": already, "now": len(claims)})
            stats["claims"] += len(claims)

        # Before reconciliation: the gaps shape the rating states it compares.
        stats.update(detect_gaps(conn, issuer_id))
        stats.update(reconcile(conn, issuer_id))
        stats["diffs"] = build_diffs(conn, issuer_id)
        conn.commit()

    return stats


if __name__ == "__main__":
    result = run(reset="--reset" in sys.argv, strict="--strict" in sys.argv,
                 reprocess="--reprocess" in sys.argv)
    for doc in result["documents"]:
        note = "" if doc.get("coverage", "complete") == "complete" else "  INCOMPLETE"
        print(f"  {doc['status']:>24}  {doc['file']:<24} id={doc['id']:<3} "
              f"claims={doc['claims']}{note}")
    for doc in result["reprocessed"]:
        print(f"\n  re-extracted {doc['file']}: extractor {doc['from']} → {doc['to']}, "
              f"{doc['was']} claims replaced by {doc['now']}")
    for doc in result["incomplete"]:
        print(f"\n  extraction incomplete: {doc['file']}")
        for reason in doc["missing"]:
            print(f"    - {reason}")
    print(f"\nclaims={result['claims']}  conflicts={result['conflicts']}  "
          f"corroborations={result['corroborations']}  resolved={result['resolved']}  "
          f"diffs={result['diffs']}")
    print(f"corpus gaps={result['gaps']} ({result['gaps_within_corpus']} within the "
          f"period our documents cover)  gaps resolved={result['gaps_resolved']}")
    for fact_key, reason in result["excluded_keys"]:
        print(f"  not reconciled: {fact_key} — {reason}")
