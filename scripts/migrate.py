"""Apply the pilot-form schema to the deployment database.

Run before the server starts (the container entrypoint does this). The public
site's only database is the one holding pilot requests, and the RDS instance is
not reachable from a laptop, so the idempotent migration is applied by the
process that can reach it.

A failure here is logged, not fatal: the landing page and demo need no database,
and POST /api/pilot already answers 503 when storage is down.
"""
from __future__ import annotations

import pathlib
import sys

import psycopg

from issuergraph.db import dsn

SQL = pathlib.Path(__file__).resolve().parent.parent / "sql"

# (migration, table it alters). A migration is applied only where its table
# exists: the demo-only database holds pilot tables and nothing else, and a
# full workspace database holds everything.
MIGRATIONS = [
    (SQL / "006_pilot_requests.sql", None),
    (SQL / "007_withdrawn_and_ended.sql", "conflict"),
]


def main() -> int:
    try:
        with psycopg.connect(dsn(), autocommit=True) as conn:
            for path, table in MIGRATIONS:
                if table and not conn.execute(
                        "SELECT to_regclass(%s)", (table,)).fetchone()[0]:
                    print(f"migrate: {path.name} not applicable here (no {table} table)")
                    continue
                conn.execute(path.read_text())
                print(f"migrate: applied {path.name}")
        return 0
    except Exception as exc:  # noqa: BLE001 — startup must not depend on the DB
        print(f"migrate: skipped, database unavailable: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
