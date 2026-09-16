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


def run(reset: bool = False) -> dict:
    fetched = fetch_missing()
    stats = {"fetched": fetched, "documents": [], "claims": 0}

    with connect() as conn:
        if reset:
            conn.execute("TRUNCATE issuer RESTART IDENTITY CASCADE")
        issuer_id = ensure_issuer(conn)

        for source in SOURCES:
            path = pathlib.Path(RAW_DIR / source.filename)
            document_id = ingest_document(conn, issuer_id, path, source.meta,
                                          retrieved_at=datetime.fromtimestamp(
                                              path.stat().st_mtime, tz=timezone.utc))

            already = conn.execute(
                "SELECT count(*) AS n FROM claim WHERE document_id = %s", (document_id,)
            ).fetchone()["n"]
            if already:
                stats["documents"].append({"file": source.filename, "id": document_id,
                                           "claims": already, "status": "cached"})
                stats["claims"] += already
                continue

            pages = pages_of(conn, document_id)
            claims = source.extractor.extract(source.meta, pages)

            # the document's own stated date, discovered during extraction
            dates = [c.as_of_date for c in claims if c.as_of_date]
            if source.meta.doc_type == "rating_rationale" and dates:
                conn.execute("UPDATE document SET published_date = %s WHERE id = %s",
                             (max(set(dates), key=dates.count), document_id))

            load_claims(conn, issuer_id, document_id, claims)
            stats["documents"].append({"file": source.filename, "id": document_id,
                                       "claims": len(claims), "status": "extracted"})
            stats["claims"] += len(claims)

        stats.update(reconcile(conn, issuer_id))
        stats["diffs"] = build_diffs(conn, issuer_id)
        conn.commit()

    return stats


if __name__ == "__main__":
    result = run(reset="--reset" in sys.argv)
    for doc in result["documents"]:
        print(f"  {doc['status']:>9}  {doc['file']:<24} id={doc['id']:<3} claims={doc['claims']}")
    print(f"\nclaims={result['claims']}  conflicts={result['conflicts']}  "
          f"corroborations={result['corroborations']}  resolved={result['resolved']}  "
          f"diffs={result['diffs']}")
    for fact_key, reason in result["excluded_keys"]:
        print(f"  not reconciled: {fact_key} — {reason}")
