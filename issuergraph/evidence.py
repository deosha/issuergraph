"""What of a source may leave this server, per publisher.

Evidence is always verified in full by the loader; this module decides how
much of it is *shown*. Under 'quote_and_link' (settings.evidence_policy) the
publisher's terms restrict redistribution, so nothing that is a copy of their
page leaves the server: every anchor's quote, the claim's text value and any
quoted diff or conflict text is cut to a short excerpt, no page is rendered or
served, and the reader is sent to the publisher's own URL with a text fragment
(#:~:text=...) that asks their browser to highlight the span.

Enforced here, server-side, rather than in the browser: the public demo
snapshot and the JSON API are themselves copies, so a cap applied only when
rendering would still redistribute the full text.
"""
from __future__ import annotations

from urllib.parse import quote

from .settings import evidence_policy

QUOTE_CAP = 200                 # characters of a quote shown under quote_and_link
FRAGMENT_WORDS = 4              # words at each end of a textStart,textEnd fragment


def excerpt(text: str | None, cap: int = QUOTE_CAP) -> tuple[str | None, bool]:
    """(short quote, truncated?). Whitespace is collapsed for display only;
    the stored anchor keeps its raw text and offsets."""
    if text is None:
        return None, False
    flat = " ".join(text.split())
    if len(flat) <= cap:
        return flat, False
    cut = flat[:cap].rsplit(" ", 1)[0] or flat[:cap]
    return cut + "…", True


def _term(words: list[str]) -> str:
    # '-', ',' and '&' delimit fragment terms and must be encoded inside one.
    return quote(" ".join(words), safe="").replace("-", "%2D")


def text_fragment(url: str, quoted: str) -> str:
    """`url` with a text fragment selecting `quoted` on the publisher's page.

    Built from the whitespace-collapsed quote, because browsers match the
    rendered text. Short quotes are matched whole; longer ones by their first
    and last words (textStart,textEnd), which is how the syntax expresses a
    range. Best-effort by nature: if the publisher's rendering differs, the
    browser simply opens the page without highlighting.
    """
    words = quoted.split()
    base = url.split("#", 1)[0]
    if not words:
        return base
    if len(words) <= 2 * FRAGMENT_WORDS:
        directive = _term(words)
    else:
        directive = f"{_term(words[:FRAGMENT_WORDS])},{_term(words[-FRAGMENT_WORDS:])}"
    return f"{base}#:~:text={directive}"


def quote_only(source_name: str | None) -> bool:
    return evidence_policy(source_name) == "quote_and_link"


def cap_for(source_name: str | None, text: str | None) -> str | None:
    """A text value as it may be shown for this publisher."""
    return excerpt(text)[0] if quote_only(source_name) else text


def may_serve_source(media_type: str | None, source_name: str | None) -> bool:
    """May we render a page image of this document, or serve its file?

    Only a PDF we are allowed to reproduce. An HTML page is never served from
    our origin — neither its bytes nor a render — whatever its publisher.
    """
    return media_type in (None, "application/pdf") and not quote_only(source_name)


def present_claim(row: dict) -> dict:
    """A claim as it may leave the server: policy, capped quotes, source link."""
    policy = evidence_policy(row.get("source_name"))
    row["evidence_policy"] = policy
    if policy != "quote_and_link":
        row["source_link"] = row.get("url")
        return row
    first = row["anchors"][0]["evidence_text"] if row["anchors"] else ""
    row["source_link"] = text_fragment(row["url"], first)
    row["value_text"], row["value_truncated"] = excerpt(row.get("value_text"))
    for a in row["anchors"]:
        a["evidence_text"], a["truncated"] = excerpt(a["evidence_text"])
        a["bbox"] = a["bbox_rects"] = None
    return row
