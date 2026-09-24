"""HTML sources: parse the bytes as received, locate text by node path.

An HTML rationale has no pages and no word geometry. Evidence is located by a
node path — lxml's getpath() on the parsed raw HTML — and a character range in
that node's text_content(), exactly as lxml returns it. Nothing is normalised
before indexing: a Word-exported page carries CRLFs, runs of tabs and &nbsp;
inside the cells, and collapsing any of it would shift every offset after it.
Extractors that want to match on collapsed whitespace map back to raw offsets
first (common.normalized_view).

Parsing must be reproducible, because the loader re-parses the stored bytes to
verify every anchor. So the bytes are decoded with the charset from the
Content-Type header they were served with (a page may declare it nowhere
else), and lxml is pinned: a different libxml2 may repair malformed HTML
differently and move a path, which the loader then reports as a mismatch
rather than silently resolving somewhere else.
"""
from __future__ import annotations

import re

from lxml import html as lxml_html

# Elements whose text is one unit of prose or one table cell. A <p> inside a
# <td> is a unit and so is the <td>: an extractor anchors whichever it needs.
UNIT_TAGS = ("p", "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "caption", "dt", "dd")
# Whole-table nodes: never an anchor for a publisher whose page we may only
# quote from, because their text is the whole table.
TABLE_TAGS = ("table", "thead", "tbody", "tfoot", "tr")

CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)


def charset_of(content_type: str | None) -> str | None:
    m = CHARSET.search(content_type or "")
    return m.group(1).lower() if m else None


def parse(data: bytes, content_type: str | None = None):
    """The element tree of `data`, decoded as its Content-Type says."""
    parser = lxml_html.HTMLParser(encoding=charset_of(content_type))
    return lxml_html.document_fromstring(data, parser=parser).getroottree()


def units(tree) -> list[dict]:
    """The text units an extractor reads, in document order.

    The HTML counterpart of a PDF page: each carries its locator and its raw
    text, so `common.anchor_in(unit, start, end)` builds the right anchor
    without the extractor knowing which kind it is.
    """
    out = []
    for element in tree.getroot().iter(*UNIT_TAGS):
        text = element.text_content()
        if text.strip():
            out.append({"kind": "html", "node_path": tree.getpath(element), "text": text})
    return out


def resolve(tree, node_path: str):
    """The one element at `node_path`, or None if it names none or several."""
    try:
        found = tree.xpath(node_path)
    except Exception:                      # a malformed path proves nothing
        return None
    return found[0] if len(found) == 1 and hasattr(found[0], "text_content") else None
