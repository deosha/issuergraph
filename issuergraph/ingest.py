"""Retrieval and page extraction.

Canonical page text is built from PyMuPDF word boxes so that every character
offset maps back to an exact rectangle. We never re-search the page for text at
highlight time.
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timezone

import pymupdf

from .db import connect
from .models import DocumentMeta

RAW_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "raw"


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_page(page: pymupdf.Page) -> tuple[str, list[list[float]]]:
    """Return (canonical_text, word_map).

    word_map entries are [char_start, char_end, x0, y0, x1, y1]. Words are
    joined by a single space within a line and a newline between lines, so
    offsets in the returned text index exactly into word_map.
    """
    words = page.get_text("words")  # x0,y0,x1,y1,word,block,line,word_no
    words.sort(key=lambda w: (w[5], w[6], w[7]))

    parts: list[str] = []
    word_map: list[list[float]] = []
    cursor = 0
    prev_line: tuple[int, int] | None = None

    for x0, y0, x1, y1, text, block, line, _ in words:
        if not text:
            continue
        if prev_line is None:
            sep = ""
        elif (block, line) != prev_line:
            sep = "\n"
        else:
            sep = " "
        if sep:
            parts.append(sep)
            cursor += len(sep)
        start = cursor
        parts.append(text)
        cursor += len(text)
        word_map.append([start, cursor, x0, y0, x1, y1])
        prev_line = (block, line)

    return "".join(parts), word_map


def rects_for_range(word_map: list[list[float]], start: int, end: int) -> tuple[list[float] | None, list[list[float]]]:
    """Union bbox and per-line rects covering the character range [start, end)."""
    hits = [w for w in word_map if w[1] > start and w[0] < end]
    if not hits:
        return None, []

    lines: list[list[float]] = []
    for _, _, x0, y0, x1, y1 in hits:
        # group words whose vertical span overlaps the current line rect
        if lines and abs(lines[-1][1] - y0) < 3 and x0 >= lines[-1][0] - 1:
            r = lines[-1]
            r[0] = min(r[0], x0)
            r[1] = min(r[1], y0)
            r[2] = max(r[2], x1)
            r[3] = max(r[3], y1)
        else:
            lines.append([x0, y0, x1, y1])

    union = [
        min(r[0] for r in lines),
        min(r[1] for r in lines),
        max(r[2] for r in lines),
        max(r[3] for r in lines),
    ]
    return union, lines


def ingest_document(conn, issuer_id: int, path: pathlib.Path, meta: DocumentMeta,
                    retrieved_at: datetime | None = None) -> int:
    """Insert a document and all its pages. Idempotent on sha256."""
    digest = sha256_of(path)
    existing = conn.execute("SELECT id FROM document WHERE sha256 = %s", (digest,)).fetchone()
    if existing:
        return existing["id"]

    doc = pymupdf.open(path)
    retrieved_at = retrieved_at or datetime.now(timezone.utc)

    row = conn.execute(
        """
        INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                              byte_size, local_path, retrieved_at, published_date, page_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """,
        (issuer_id, meta.doc_type, meta.source_name, meta.title, meta.url, digest,
         path.stat().st_size, str(path), retrieved_at, meta.published_date, doc.page_count),
    ).fetchone()
    document_id = row["id"]

    with conn.cursor() as cur:
        for page_no in range(1, doc.page_count + 1):
            page = doc[page_no - 1]
            text, word_map = build_page(page)
            cur.execute(
                """
                INSERT INTO document_page (document_id, page_no, text, width, height, rotation, word_map)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (document_id, page_no, text, page.rect.width, page.rect.height,
                 page.rotation, psycopg_json(word_map)),
            )
    doc.close()
    return document_id


def psycopg_json(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)
