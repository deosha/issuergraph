"""GST registration certificate (Form GST REG-06) and Udyam registration certificate.

Deterministic: each field is a printed label followed, on the same line or the
next, by its value. Dates found here are what business vintage is computed
from; each is anchored from its label to its value.
"""
from __future__ import annotations

import re

from .bank import parse_date
from .extract import XFact
from .pages import AnchorError, Page, anchor_span

DATE = r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}|\d{1,2}[/\-. ][A-Za-z]{3,9}[/\-. ]\d{4})"
GST = [
    ("registration_number", "text", r"Registration Number\s*:?\s*([0-9]{2}[A-Z0-9]{13})"),
    ("constitution", "text", r"Constitution of Business\s*:?\s*([A-Za-z ]{3,40}?)\s*(?:\n|$)"),
    ("liability_date", "date", r"Date of Liability\s*:?\s*" + DATE),
    ("validity_from", "date", r"Period of Validity\s*:?\s*From\s*:?\s*" + DATE),
    ("registration_type", "text", r"Type of Registration\s*:?\s*([A-Za-z ]{3,30}?)\s*(?:\n|$)"),
]
UDYAM = [
    ("registration_number", "text", r"(UDYAM-[A-Z]{2}-\d{2}-\d{7})"),
    ("enterprise_type", "text", r"Type of Enterprise\s*:?\s*([A-Za-z]+)"),
    ("major_activity", "text", r"Major Activity\s*:?\s*([A-Za-z ]{3,30}?)\s*(?:\n|$)"),
    ("incorporation_date", "date", r"Date of Incorporation\s*/?\s*Registration of Enterprise\s*:?\s*" + DATE),
    ("commencement_date", "date", r"Date of Commencement of Production\s*/?\s*Business Operation\s*:?\s*" + DATE),
    ("udyam_date", "date", r"Date of Udyam Registration\s*:?\s*" + DATE),
]


def extract(pages: list[Page], kind: str) -> tuple[list[XFact], list[str]]:
    specs = GST if kind == "gst_certificate" else UDYAM
    facts: list[XFact] = []
    for field, vkind, pattern in specs:
        rx = re.compile(pattern, re.I)
        for p in pages:
            m = rx.search(p.text)
            if not m:
                continue
            raw = m.group(1).strip()
            value = parse_date(raw) if vkind == "date" else " ".join(raw.split())
            if value is None:
                continue
            try:
                anchor = anchor_span(p, m.start(), m.end(1))
            except AnchorError:
                continue
            facts.append(XFact(field, vkind, value, "parser", True, anchor=anchor, page_no=p.page_no))
            break
    have = {f.field for f in facts}
    dates = {"liability_date", "validity_from"} if kind == "gst_certificate" else \
        {"incorporation_date", "commencement_date", "udyam_date"}
    issues = []
    if "registration_number" not in have:
        issues.append("Registration number not found.")
    if not dates & have:
        issues.append("No registration or commencement date found; vintage cannot be computed "
                      "from this certificate.")
    return facts, issues
