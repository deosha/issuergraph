"""Shared parsing helpers.

Every helper works on the *canonical page text* and returns character offsets
into it, because those offsets are what becomes evidence.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from ..models import Anchor

NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})\b"
)
SHORT_DATE_RE = re.compile(r"^([A-Z][a-z]{2})-(\d{2})-(\d{4})$")
DMY_RE = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b")


def parse_amount(token: str) -> Decimal | None:
    token = token.strip()
    if not token or token in {"-", "NA", "N.A."}:
        return None
    if not NUM_RE.match(token):
        return None
    return Decimal(token.replace(",", ""))


def parse_short_date(token: str) -> date | None:
    m = SHORT_DATE_RE.match(token.strip())
    if not m:
        return None
    try:
        return datetime.strptime(token.strip(), "%b-%d-%Y").date()
    except ValueError:
        return None


def find_published_date(text: str) -> date | None:
    m = LONG_DATE_RE.search(text)
    if m:
        return datetime.strptime(m.group(0), "%B %d, %Y").date()
    m = DMY_RE.search(text)
    if m:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y").date()
    return None


def quarter_key(d: date | None) -> str:
    if d is None:
        return "unknown"
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def lines_with_offsets(text: str, start: int = 0, end: int | None = None):
    """Yield (line_text, char_start, char_end) for the slice [start, end)."""
    end = len(text) if end is None else end
    pos = start
    for raw in text[start:end].split("\n"):
        yield raw, pos, pos + len(raw)
        pos += len(raw) + 1


def anchor(page_no: int, text: str, start: int, end: int) -> Anchor:
    """Build an anchor, trimming whitespace while keeping offsets exact."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return Anchor(page_no=page_no, char_start=start, char_end=end,
                  evidence_text=text[start:end])


INSTRUMENT_TYPES = (
    ("commercial paper", "commercial_paper"),
    ("subordinated debt", "subordinated_debt"),
    ("subordinated liabilities", "subordinated_debt"),
    ("perpetual debt", "perpetual_debt"),
    ("bank lines", "bank_facility"),
    ("bank facilities", "bank_facility"),
    ("bank loan", "bank_facility"),
    ("market linked debenture", "mld"),
    ("equity linked debenture", "mld"),
    ("debt securities", "debt_securities"),
    ("borrowings (other than debt securities)", "bank_facility"),
    ("debenture", "ncd"),
    ("ncd", "ncd"),
)


def classify_instrument(name: str) -> str:
    lowered = name.lower()
    for needle, kind in INSTRUMENT_TYPES:
        if needle in lowered:
            return kind
    return "other"


# Instrument classes that are rated on the short-term scale (A1+ … A4), where a
# long-term grade (AA, AA+) is not a comparable quantity at all.
SHORT_TERM_CLASSES = ("commercial_paper",)


def rating_identity(instrument_name: str, grade: str | None = None) -> tuple[str, str]:
    """(instrument_class, term) — what makes two agencies' ratings comparable.

    Agencies name the same instrument differently and rate several instruments
    in one action, so neither the publisher's wording nor "the first row in the
    table" identifies what is being rated. The class does, and the term keeps
    the two rating scales apart: ICRA's A1+ on commercial paper and its AA on
    debentures are not a disagreement with anybody, they are different scales.
    """
    instrument_class = classify_instrument(instrument_name)
    if instrument_class in SHORT_TERM_CLASSES:
        return instrument_class, "short_term"
    # A grade on the short-term scale settles it even when the name does not.
    if grade and re.fullmatch(r"A[1-4]\+?", grade.strip()):
        return instrument_class, "short_term"
    return instrument_class, "long_term"


RATING_GRADE_RE = re.compile(
    r"(?:PP-MLD)?\[ICRA\]\s*(?P<grade>A[1-4]\+?|A{1,3}\+?-?|BBB[+-]?|BB[+-]?|B[+-]?|C|D)"
    r"|CARE\s+(?P<grade2>A[1-4]\+?|A{1,3}\+?-?|BBB[+-]?)"
    r"|BWR\s+(?P<grade3>A[1-4]\+?|A{1,3}\+?-?|BBB[+-]?)"
)
OUTLOOK_RE = re.compile(r"\((Stable|Negative|Positive|Developing)\)|/\s*(Stable|Negative|Positive)",
                        re.IGNORECASE)
WATCH_RE = re.compile(
    r"Rating\s+Watch\s+with\s+(Negative|Positive|Developing)\s+Implications|\((RWN|RWP|RWD)\)",
    re.IGNORECASE,
)
WATCH_EXPANSION = {"RWN": "Negative", "RWP": "Positive", "RWD": "Developing"}


def normalise_grade(raw: str) -> str | None:
    m = RATING_GRADE_RE.search(raw)
    if not m:
        return None
    return m.group("grade") or m.group("grade2") or m.group("grade3")


def parse_outlook(raw: str) -> str | None:
    m = OUTLOOK_RE.search(raw)
    if not m:
        return None
    value = m.group(1) or m.group(2)
    return value.capitalize() if value else None


def parse_watch(raw: str) -> str | None:
    m = WATCH_RE.search(raw)
    if not m:
        return None
    if m.group(1):
        return f"Watch with {m.group(1).capitalize()} Implications"
    return f"Watch with {WATCH_EXPANSION[m.group(2).upper()]} Implications"


ACTION_WORDS = ("reaffirmed", "withdrawn", "assigned", "downgraded", "upgraded",
                "placed on", "revised", "confirmed", "removed")


def parse_action(raw: str) -> str | None:
    lowered = raw.lower()
    found = [w for w in ACTION_WORDS if w in lowered]
    return "; ".join(found) if found else None
