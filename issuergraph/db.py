"""Postgres access. Raw SQL by design — the schema is the model."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("ISSUERGRAPH_DSN", "postgresql:///issuergraph")


@contextmanager
def connect():
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        yield conn


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None
