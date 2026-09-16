"""The pipeline: fetch → ingest → extract → load → reconcile → diff.

An ordered function. Each stage's output is the next stage's input; failures
abort the transaction rather than degrade silently.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
from datetime import datetime, timezone

from .corpus import ISSUER, SOURCES
from .db import connect
from .diff import build_diffs
from .extractors import coverage as coverage_check
from .ingest import RAW_DIR, ingest_document
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
        subprocess.run(["curl", "-sSL", "--fail", "--max-time", "180", "-A", UA,
                        "-o", str(path), source.url], check=True)
        fetched.append(source.filename)
    return fetched


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
    return conn.execute(
        "SELECT page_no, text FROM document_page WHERE document_id = %s ORDER BY page_no",
        (document_id,),
    ).fetchall()


class IncompleteExtraction(RuntimeError):
    """Raised in strict mode when a document does not meet its own declaration."""


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
            document_id = ingest_document(conn, issuer_id, path, source.meta,
                                          retrieved_at=datetime.fromtimestamp(
                                              path.stat().st_mtime, tz=timezone.utc))

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

            if already:
                # Replaced, not appended: two versions of the same fact from one
                # document are not two sources agreeing, they are one parser
                # changing its mind, and reconciliation must never see both.
                conn.execute("DELETE FROM claim WHERE document_id = %s", (document_id,))

            load_claims(conn, issuer_id, document_id, claims)
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
    for fact_key, reason in result["excluded_keys"]:
        print(f"  not reconciled: {fact_key} — {reason}")
