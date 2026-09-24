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
from .wording import PUBLISHER_NAME, publisher, readable

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


POLICY_NOTE = ("{source}'s terms restrict redistribution, so this is shown as a short "
               "quote. Open it on {source}'s page to read it in context.")
# Words that mean "we do not know", which a reader must never see in place of
# a fact. The demo build refuses a panel containing one (export_demo.py).
PLACEHOLDERS = ("unknown", "null", "none", "undefined", "nan")


def _day(d) -> str:
    return d.strftime("%d %b %Y") if d else ""


def panel_lines(row: dict) -> list[str]:
    """The evidence panel's descriptive lines, as a reader sees them.

    Built here so the demo and the live product say the same thing and the
    snapshot build can check the exact strings. A web page has no page number
    or page count; a rating has no accounting basis — a line that does not
    apply is left out, never filled with a placeholder.
    """
    first = row["anchors"][0] if row["anchors"] else None
    published = (f"published {_day(row['published_date'])}" if row.get("published_date")
                 else "publication date not stated")
    head = [row.get("title"), publisher(row.get("source_name")), published]
    if row.get("source_updated_on"):
        head.append(f"updated by the publisher {_day(row['source_updated_on'])}")
    where = []
    if first and first.get("kind") == "html":
        where.append(f"web page · chars {first['char_start']}–{first['char_end']}")
    elif first and first.get("page_no"):
        of = f" of {row['page_count']}" if row.get("page_count") else ""
        where.append(f"page {first['page_no']}{of} · chars {first['char_start']}–{first['char_end']}")
    if row.get("basis") in ("standalone", "consolidated"):
        where.append(f"basis {row['basis']}")
    if row.get("as_of_date"):
        where.append(f"as at {_day(row['as_of_date'])}")
    provenance = [row.get("extractor") and f"{row['extractor']} v{row['extractor_version']}",
                  row.get("sha256") and f"sha256 {row['sha256'][:16]}…",
                  row.get("retrieved_at") and f"retrieved {_day(row['retrieved_at'])}"]
    lines = [" · ".join(filter(None, part)) for part in (head, where, provenance)]
    if row.get("source_changed_at"):
        lines.append(f"changed at source since retrieval (noticed {_day(row['source_changed_at'])}); "
                     "the stored copy remains the evidence of record")
    return [line for line in lines if line]


def placeholder_in(text: str) -> str | None:
    """The first placeholder word in `text`, if any (whole words, any case)."""
    words = {w.strip(".,;:()[]\"'").lower() for w in text.split()}
    return next((p for p in PLACEHOLDERS if p in words), None)


def present_claim(row: dict) -> dict:
    """A claim as it may leave the server: policy, capped quotes, source links,
    and the panel's lines."""
    policy = evidence_policy(row.get("source_name"))
    row["evidence_policy"] = policy
    row["plain_url"] = row.get("url")
    row["subject"] = readable(row.get("subject"))
    if policy != "quote_and_link":
        row["source_link"] = row.get("url")
        row["panel_lines"] = panel_lines(row)
        return row
    first = row["anchors"][0]["evidence_text"] if row["anchors"] else ""
    row["source_link"] = text_fragment(row["url"], first)
    row["value_text"], row["value_truncated"] = excerpt(row.get("value_text"))
    for a in row["anchors"]:
        a["evidence_text"], a["truncated"] = excerpt(a["evidence_text"])
        a["bbox"] = a["bbox_rects"] = None
    row["policy_note"] = POLICY_NOTE.format(source=publisher(row["source_name"]))
    row["panel_lines"] = panel_lines(row)
    return row
