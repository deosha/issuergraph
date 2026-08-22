"""Dry-run an extractor against a PDF without touching the database.

    python scripts/tryextract.py icra data/raw/icra_137962.pdf
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from issuergraph.ingest import build_page  # noqa: E402
from issuergraph.models import DocumentMeta  # noqa: E402


def pages_of(path):
    doc = pymupdf.open(path)
    out = []
    for i, page in enumerate(doc, 1):
        text, word_map = build_page(page)
        out.append({"page_no": i, "text": text, "word_map": word_map})
    return out


if __name__ == "__main__":
    mod = __import__(f"issuergraph.extractors.{sys.argv[1]}", fromlist=["extract"])
    pages = pages_of(sys.argv[2])
    meta = DocumentMeta(doc_type="rating_rationale", source_name="dry-run",
                        title="dry-run", url="dry-run")
    claims = mod.extract(meta, pages)
    for c in claims:
        a = c.anchors[0]
        value = c.value_numeric if c.value_numeric is not None else c.value_text
        print(f"[{c.claim_type}] {c.fact_key}")
        print(f"    value={value!r} p{a.page_no}[{a.char_start}:{a.char_end}] "
              f"ev={a.evidence_text[:110]!r}")
        for anchor in c.anchors:
            page_text = pages[anchor.page_no - 1]["text"]
            assert page_text[anchor.char_start:anchor.char_end] == anchor.evidence_text, \
                f"OFFSET MISMATCH on {c.fact_key}"
    print("TOTAL", len(claims))
