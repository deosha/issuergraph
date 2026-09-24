"""CARE Ratings press-release extractor."""
from __future__ import annotations

import re

from ..effective import is_withdrawal
from ..models import DebtDetail, ExtractedClaim, RatingDetail
from . import history
from .coverage import Coverage, rating_rationale_coverage
from .common import (anchor, classify_instrument, find_published_date, lines_with_offsets,
                     normalise_grade, parse_action, parse_amount, parse_outlook, parse_watch,
                     quarter_key, rating_identity)

EXTRACTOR = "care_press_release"
VERSION = "2.6.0"
AGENCY = "CARE"

TABLE_HEAD = "Facilities/Instruments"
TABLE_END = "Details of instruments/facilities"
RATING_CELL = re.compile(r"^(?:CARE\s+[A-D]|-$)")
# What may follow "CARE AA" inside the rating cell itself: the watch code or
# the outlook, wrapped onto the next line by the table layout.
RATING_CONTINUATION = re.compile(r"^(?:\((?:RWN|RWP|RWD)\)|(?:Stable|Negative|Positive|Developing)\)?)$")
NUMERIC_CELL = re.compile(r"^(?:[\d,]+\.\d{2}|-)$")
HEADER_CELL = re.compile(r"^(Facilities/Instruments|Amount \(₹|crore\)|Rating\d?|Rating Action)$")
CARE_OUTLOOK = re.compile(r";\s*(Stable|Negative|Positive|Developing)\b")
PREVIOUS_GRADE = re.compile(r"\bfrom\s+(CARE\s+\S+)")
LIQUIDITY_RE = re.compile(r"(?m)^Liquidity:\s*(?P<grade>[A-Za-z ]+)\n(?P<body>[^\n]+)")
FACTORS_RE = re.compile(r"(?m)^(?P<kind>Positive|Negative) factors\s*\n(?P<body>(?:•\n[^\n]+\n?)+)")
ANNEXURE_HEAD = "Annexure-1: Details of instruments/facilities"
ANNEXURE_END = "Annexure-2"
SIZE_CELL = re.compile(r"^[\d,]+\.\d{2}$")
HISTORY_HEAD = "Annexure-2: Rating history"
HISTORY_END = ("LT: Long term", "Annexure-3")
PAGE_FURNITURE = re.compile(r"^(CARE Ratings Ltd\.|Press Release)$")
# "1)CARE AA (RWN) (13-Apr-24)" or "1)Withdrawn (21-May-20)", over line breaks.
HISTORY_ENTRY = re.compile(r"(?s)(?<![\w)])\d\)\s*(?P<body>.*?)\((?P<date>\d{1,2}-[A-Za-z]{3}-\s*\d{2})\)")
HISTORY_MARKER = re.compile(r"(?<![\w)])\d\)")
LABEL_SLACK = 3.0       # points; the label column is left-aligned at the table edge


def _span(lines):
    return (lines[0][1], lines[-1][2]) if lines else None


def _joined(lines):
    return " ".join(l.strip() for l, _, _ in lines) if lines else None


def _word_map(page: dict):
    word_map = page.get("word_map")
    if word_map is None:
        raise ValueError("the CARE extractor needs the page word_map to find table columns")
    return word_map


def _label_groups(text: str, word_map, start: int, end: int) -> list[dict]:
    """Split a table into rows at each line in its first (label) column.

    The first column is left-aligned at the table edge — the leftmost line
    start in the table — and every other cell is indented. Not the header's
    position: Annexure-1 centres "Name of the Instrument" over names that sit
    further left.
    """
    line_x = {int(w[0]): w[2] for w in word_map}
    lines = list(lines_with_offsets(text, start, end))
    starts = [line_x[ls] for line, ls, _ in lines if line.strip() and ls in line_x]
    label_x = min(starts) if starts else None
    groups: list[dict] = []
    for line, ls, le in lines:
        stripped = line.strip()
        if not stripped or HEADER_CELL.match(stripped):
            continue
        x = line_x.get(ls)
        if x is not None and label_x is not None and abs(x - label_x) <= LABEL_SLACK:
            if not groups or groups[-1]["cells"]:
                groups.append({"label": [], "cells": []})
            groups[-1]["label"].append((line, ls, le))
        elif groups:
            groups[-1]["cells"].append((line, ls, le))
    return groups


def _annexure(pages: list[dict]) -> list[dict]:
    """Annexure-1 rows that carry an issue size: name, size, and their spans.

    The summary table sometimes names an instrument only generically — "Long
    Term Instruments" — while Annexure-1 of the same release lists it by name
    and ISIN ("Subordinated debt INE866I08246 ... 100.00"). Header rows carry
    no size and fall out.
    """
    entries = []
    for page in pages:
        text = page["text"]
        start = text.find(ANNEXURE_HEAD)
        if start == -1:
            continue
        start = text.find("\n", start) + 1
        end = text.find(ANNEXURE_END, start)
        end = len(text) if end == -1 else end
        for group in _label_groups(text, _word_map(page), start, end):
            sizes = [c for c in group["cells"] if SIZE_CELL.match(c[0].strip())]
            if len(sizes) != 1:
                continue
            entries.append({"page_no": page["page_no"], "text": text,
                            "name": _joined(group["label"]),
                            "name_span": _span(group["label"]),
                            "size": parse_amount(sizes[0][0].strip()),
                            "size_span": (sizes[0][1], sizes[0][2])})
    return entries


def _named_in_annexure(annexure: list[dict], amount,
                       taken: set[str] = frozenset()) -> dict | None:
    """The one Annexure-1 instrument of this size with a recognisable class.

    Exactly one, or nothing: two candidates of the same size is a guess, and
    the row stays unclassified rather than borrowing a class it cannot prove.

    A withdrawn row has no amount ("-"); the annexure then lists the withdrawn
    instruments at 0.00. Among those, the one whose class no other row of the
    table already names is this row's — "Long Term Instruments" beside "Long
    Term Bank Facilities" is the Subordinate Debt (CARE, 20 Sep 2024).
    """
    if amount is None:
        hits = [e for e in annexure if e["size"] == 0
                and classify_instrument(e["name"]) not in ("other", *taken)]
    else:
        hits = [e for e in annexure
                if e["size"] == amount and classify_instrument(e["name"]) != "other"]
    return hits[0] if len(hits) == 1 else None


def _rows(page: dict):
    """One dict per row of the summary rating table.

    Rows are delimited by the label column, read from the word boxes: the
    first column is left-aligned at the table edge and every other cell is
    indented. The text alone cannot say where one row's action wording ends
    and the next row's label begins — "Placed on Rating Watch with Developing
    / Implications / Long Term Long Term / Instruments" is four lines either
    way — and guessing at it glued one row's action onto the next row's
    label. Within a row the cells come in column order: amount, rating,
    action. The rating cell is kept apart from the action wording because the
    wording can name the watch or grade it moved *from*.
    """
    text, word_map = page["text"], _word_map(page)
    start = text.find(TABLE_HEAD)
    if start == -1:
        return
    end = text.find(TABLE_END, start)
    end = len(text) if end == -1 else end
    groups = _label_groups(text, word_map, start, end)

    for group in groups:
        cells = list(group["cells"])
        amount = cells.pop(0) if cells and NUMERIC_CELL.match(cells[0][0].strip()) else None
        rating: list = []
        if cells and RATING_CELL.match(cells[0][0].strip()):
            rating.append(cells.pop(0))
            while cells and RATING_CONTINUATION.match(cells[0][0].strip()):
                rating.append(cells.pop(0))
        yield {
            "label": _joined(group["label"]),
            "label_span": _span(group["label"]),
            "amount": parse_amount(amount[0].strip()) if amount else None,
            "amount_span": (amount[1], amount[2]) if amount else None,
            "rating_text": _joined(rating),
            "rating_span": _span(rating),
            "action_text": _joined(cells),
            "action_span": _span(cells),
        }


def _action(action_text: str | None) -> str | None:
    if not action_text:
        return None
    found = parse_action(action_text)
    if found is None and "revision" in action_text.casefold():
        return "revised"        # "Revision in credit watch from ... to ..."
    return found


def _history(doc_meta, pages: list[dict]):
    """(history claims, unreadable) from Annexure-2.

    Rows come from the shared table reader; each entry carries its own date,
    so it is read from the row's text run by run (one page's contiguous lines
    at a time, so offsets stay exact). A numbered entry that does not parse is
    reported, not dropped.
    """
    header, body = history.table_lines(pages, HISTORY_HEAD, HISTORY_END, PAGE_FURNITURE)
    texts = {p["page_no"]: p["text"] for p in pages}
    entries, unreadable = [], []
    for row in history.rows(header, body):
        for run in history.runs(row["cells"]):
            text = texts[run[0]["page_no"]]
            begin, end = run[0]["start"], run[-1]["end"]
            chunk = text[begin:end]
            matched = 0
            for m in HISTORY_ENTRY.finditer(chunk):
                matched += 1
                when = history.parse_dmy_short(m.group("date"))
                body_text = " ".join(m.group(0).split())
                read = history.read_rating(body_text)
                if when is None or not (read["grade"] or read["withdrawn"]):
                    unreadable.append(f"{row['name']!r}: {body_text!r}")
                    continue
                line = {"page_no": run[0]["page_no"], "start": begin + m.start(),
                        "end": begin + m.end()}
                entries.append({"row": row, "date": when, "date_lines": [],
                                "rating_lines": [line], "rating_text": body_text, **read})
            missed = len(HISTORY_MARKER.findall(chunk)) - matched
            if missed > 0:
                unreadable.append(f"{row['name']!r}: {missed} numbered entries without a date")
    pub = getattr(doc_meta, "published_date", None) or find_published_date(pages[0]["text"])
    return history.history_claims(AGENCY, EXTRACTOR, VERSION, pages, entries, pub), unreadable


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """A CARE press release carries the facilities table, the liquidity
    paragraph and both directions of rating sensitivities — and every row of
    the table must become a rating or a withdrawal. "Some rating was found"
    passed a table whose withdrawn debentures row had silently vanished."""
    # A release that only withdraws ratings has no sensitivities to state
    # (CARE, 20 Sep 2024): every other requirement still applies.
    table = [c for c in claims if c.claim_type == "rating"]
    withdrawal_only = bool(table) and all(
        "withdrawn" in (c.rating.action or "") for c in table)
    cov = rating_rationale_coverage(AGENCY, claims, () if withdrawal_only else (
        (f"rating_sensitivity|{AGENCY}|positive", "positive rating sensitivities"),
        (f"rating_sensitivity|{AGENCY}|negative", "negative rating sensitivities"),
    ))
    cov.merge(history.history_coverage(AGENCY, claims, _history(doc_meta, pages)[1]))
    rated = {(c.anchors[0].page_no, c.anchors[0].char_start)
             for c in claims if c.claim_type == "rating"}
    for page in pages:
        for row in _rows(page):
            cov.require((page["page_no"], row["label_span"][0]) in rated,
                        f"{AGENCY}: table row {row['label']!r} on page {page['page_no']} "
                        f"yielded no rating or withdrawal")
    return cov


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    pub = doc_meta.published_date or find_published_date(pages[0]["text"])
    qkey = quarter_key(pub)
    annexure = _annexure(pages)

    for page in pages:
        pno, text = page["page_no"], page["text"]

        rows = list(_rows(page))
        named_classes = {rating_identity(r["label"])[0] for r in rows} - {"other"}
        for row in rows:
            if row["rating_text"] is None or row["amount_span"] is None:
                continue                    # coverage names the row
            grade = normalise_grade(row["rating_text"])
            action = _action(row["action_text"])
            if not grade and not is_withdrawal(action):
                continue
            label = row["label"]
            slug = f"{label.lower()}|{row['amount']}"
            anchors = [anchor(pno, text, *row["label_span"]),
                       anchor(pno, text, *row["amount_span"]),
                       anchor(pno, text, *row["rating_span"])]
            if row["action_span"]:
                anchors.append(anchor(pno, text, *row["action_span"]))
            outlook = CARE_OUTLOOK.search(row["rating_text"])
            previous = PREVIOUS_GRADE.search(row["action_text"] or "")
            instrument_class, term = rating_identity(label, grade)
            if instrument_class == "other":
                # Classified from the annexure line that names it, and that
                # line is evidence on the claim: the class is proven, not
                # inferred from the size alone.
                named = _named_in_annexure(annexure, row["amount"], named_classes)
                if named:
                    instrument_class, term = rating_identity(named["name"], grade)
                    anchors += [anchor(named["page_no"], named["text"], *named["name_span"]),
                                anchor(named["page_no"], named["text"], *named["size_span"])]

            claims.append(ExtractedClaim(
                claim_type="rating",
                fact_key=f"rating_instrument|{AGENCY}|{slug}|{qkey}",
                subject=f"{label} — {AGENCY} rating",
                value_text=row["rating_text"] if grade else row["action_text"],
                as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION, anchors=anchors,
                rating=RatingDetail(agency=AGENCY, instrument=label,
                                    rated_amount_cr=row["amount"], rating=grade,
                                    instrument_class=instrument_class, term=term,
                                    outlook=(outlook.group(1).capitalize() if outlook
                                             else parse_outlook(row["rating_text"])),
                                    watch=parse_watch(row["rating_text"]),
                                    action=action,
                                    previous_rating=(normalise_grade(previous.group(1))
                                                     if previous else None),
                                    action_date=pub),
            ))

            if row["amount"] is not None:
                claims.append(ExtractedClaim(
                    claim_type="debt_instrument",
                    fact_key=f"rated_amount|{AGENCY}|{slug}|{qkey}",
                    subject=f"{label} (rated amount)",
                    value_numeric=row["amount"], value_unit="INR_CRORE", as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=anchors[:2],
                    debt=DebtDetail(instrument_name=label,
                                    instrument_type=instrument_class,
                                    amount_cr=row["amount"], as_of_date=pub),
                ))

        for m in LIQUIDITY_RE.finditer(text):
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_assessment|{AGENCY}",
                subject=f"{AGENCY} liquidity assessment",
                value_text=m.group("grade").strip(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("grade"), m.end("grade")),
                         anchor(pno, text, m.start("body"), m.end("body"))]))
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_detail|{AGENCY}",
                subject=f"{AGENCY} liquidity narrative",
                value_text=m.group("body").strip(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("body"), m.end("body"))]))

        for m in FACTORS_RE.finditer(text):
            kind = m.group("kind").lower()
            body = " ".join(line.strip() for line in m.group("body").split("\n")
                            if line.strip() and line.strip() != "•")
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"rating_sensitivity|{AGENCY}|{kind}",
                subject=f"{AGENCY} {kind} rating factors", value_text=body, as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("body"), m.end("body"))]))

    claims += _history(doc_meta, pages)[0]
    return claims
