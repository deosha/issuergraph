"""Page text and anchors for Assess documents.

Page text is built exactly as the issuer pipeline builds it
(`ingest.build_page`): PyMuPDF word boxes joined by spaces within a line and
newlines between lines, with each word's character range and rectangle in
`word_map`. A page with no text layer is sent to Textract (when configured);
its WORD boxes are written into the same `word_map` shape, so everything
downstream — anchors, highlighting — is identical whatever produced the text.

An anchor here is a list of character spans into one page's text plus the
rectangles of the words they cover. It is verified by construction: the
evidence text is *re-read* from the page at the stored offsets, never taken
from the extractor's own idea of what it saw.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from ..extractors.common import normalized_view, raw_span
from ..ingest import build_page, rects_for_range

MIN_TEXT_CHARS = 25        # fewer characters than this on a page = treat as scanned
OCR_DPI = 200


@dataclass
class Word:
    text: str
    start: int
    end: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Page:
    page_no: int
    text: str
    width: float
    height: float
    word_map: list
    text_source: str = "pdf_text"

    def words(self) -> list[Word]:
        return [Word(self.text[int(s):int(e)], int(s), int(e), x0, y0, x1, y1)
                for s, e, x0, y0, x1, y1 in self.word_map]


class AnchorError(ValueError):
    """An anchor that does not re-read to its evidence text."""


class PasswordProtected(ValueError):
    """The PDF needs a password before any text can be read."""


def pdf_pages(path: str, ocr=None) -> tuple[list[Page], int]:
    """(pages, number of pages that went to OCR).

    `ocr` is a callable (png_bytes, width, height) -> (text, word_map), or None.
    A page without a text layer and without OCR keeps its empty text; the
    extractor then has nothing to read there and the coverage check says so.
    """
    doc = pymupdf.open(path)
    if doc.needs_pass:
        doc.close()
        raise PasswordProtected(
            "Password-protected PDF. Unlock it once with: python -m issuergraph.assess unlock "
            "<document id> (the password is asked for, used once, and never stored).")
    pages, ocr_count = [], 0
    try:
        for i in range(doc.page_count):
            page = doc[i]
            text, word_map = build_page(page)
            source = "pdf_text"
            if len(text.strip()) < MIN_TEXT_CHARS and ocr is not None:
                png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
                text, word_map = ocr(png, page.rect.width, page.rect.height)
                source = "textract"
                ocr_count += 1
            pages.append(Page(i + 1, text, page.rect.width, page.rect.height, word_map, source))
    finally:
        doc.close()
    return pages, ocr_count


def needs_ocr(pages: list[Page]) -> list[int]:
    return [p.page_no for p in pages if len(p.text.strip()) < MIN_TEXT_CHARS]


# --- anchors ------------------------------------------------------------------

def anchor_words(page: Page, words: list[Word]) -> dict:
    """Anchor a set of words on one page (e.g. a statement row's cells)."""
    if not words:
        raise AnchorError("no words to anchor")
    ordered = sorted(words, key=lambda w: w.start)
    spans = [[w.start, w.end] for w in ordered]
    return _anchor(page, spans)


def anchor_span(page: Page, start: int, end: int) -> dict:
    return _anchor(page, [[start, end]])


def _anchor(page: Page, spans: list[list[int]]) -> dict:
    text = page.text
    for s, e in spans:
        if not (0 <= s < e <= len(text)):
            raise AnchorError(f"span {s}:{e} outside page {page.page_no}")
    evidence = " ".join(text[s:e] for s, e in spans)
    rects = []
    for s, e in spans:
        rects.extend(rects_for_range(page.word_map, s, e)[1])
    if not rects:
        raise AnchorError(f"span on page {page.page_no} covers no word box")
    return {"page_no": page.page_no, "spans": spans, "rects": rects, "evidence_text": evidence}


def verify(page_text: str, spans: list, evidence_text: str) -> None:
    """Raise unless the spans re-read to exactly `evidence_text`."""
    for s, e in spans:
        if not (0 <= s < e <= len(page_text)):
            raise AnchorError("span out of bounds")
    if " ".join(page_text[s:e] for s, e in spans) != evidence_text:
        raise AnchorError("evidence text does not match the page at the stored offsets")


def find_quote(page: Page, quote: str) -> list[tuple[int, int]]:
    """Every occurrence of `quote` on the page, matched on collapsed
    whitespace and case-insensitively, as raw (start, end) offsets."""
    needle = " ".join(quote.split()).casefold()
    if len(needle) < 2:
        return []
    view, index = normalized_view(page.text)
    hay = view.casefold()
    hits, pos = [], 0
    while True:
        i = hay.find(needle, pos)
        if i < 0:
            return hits
        hits.append(raw_span(index, i, i + len(needle)))
        pos = i + 1


AMOUNT_TOKEN = re.compile(r"-?\(?[\d,]+(?:\.\d+)?\)?")


def amount_in(quote: str, value) -> bool:
    """Does the quoted text contain this amount as written (ignoring commas)?"""
    want = f"{float(value):.2f}"
    for m in AMOUNT_TOKEN.finditer(quote):
        token = m.group(0).strip("()").replace(",", "")
        try:
            if f"{abs(float(token)):.2f}" == f"{abs(float(want)):.2f}":
                return True
        except ValueError:
            continue
    return False
