"""CARE Ratings press-release extractor."""
from __future__ import annotations

import re

from ..models import DebtDetail, ExtractedClaim, RatingDetail
from .coverage import Coverage, rating_rationale_coverage
from .common import (anchor, classify_instrument, find_published_date, lines_with_offsets,
                     normalise_grade, parse_action, parse_amount, parse_outlook, parse_watch,
                     quarter_key, rating_identity)

EXTRACTOR = "care_press_release"
VERSION = "2.1.0"
AGENCY = "CARE"

TABLE_HEAD = "Facilities/Instruments"
TABLE_END = "Details of instruments/facilities"
RATING_CELL = re.compile(r"^CARE\s+[A-D]")
NUMERIC_CELL = re.compile(r"^(?:[\d,]+\.\d{2}|-)$")
HEADER_CELL = re.compile(r"^(Facilities/Instruments|Amount \(₹|crore\)|Rating\d?|Rating Action)$")
LIQUIDITY_RE = re.compile(r"(?m)^Liquidity:\s*(?P<grade>[A-Za-z ]+)\n(?P<body>[^\n]+)")
FACTORS_RE = re.compile(r"(?m)^(?P<kind>Positive|Negative) factors\s*\n(?P<body>(?:•\n[^\n]+\n?)+)")


def _rows(text: str):
    start = text.find(TABLE_HEAD)
    if start == -1:
        return
    end = text.find(TABLE_END, start)
    end = len(text) if end == -1 else end
    rows = list(lines_with_offsets(text, start, end))

    label_lines: list[tuple[str, int, int]] = []
    amount_row: tuple[str, int, int] | None = None
    i = 0
    while i < len(rows):
        line, ls, le = rows[i]
        stripped = line.strip()
        if not stripped or HEADER_CELL.match(stripped):
            i += 1
            continue
        if RATING_CELL.match(stripped):
            block = []
            block_start, block_end = ls, le
            while i < len(rows):
                cell = rows[i][0].strip()
                if cell and (NUMERIC_CELL.match(cell) or HEADER_CELL.match(cell)):
                    break
                if cell:
                    block.append(cell)
                    block_end = rows[i][2]
                i += 1
                # a new label begins once we have consumed the action wording
                if i < len(rows) and rows[i][0].strip() and not rows[i][0].strip()[0].isupper():
                    continue
                if block and block[-1].rstrip().endswith(("Implications", "Stable", "Negative",
                                                          "Positive", "Reaffirmed", "reaffirmed",
                                                          "Assigned", "assigned")):
                    break
            if label_lines and amount_row:
                yield {
                    "label": " ".join(l.strip() for l, _, _ in label_lines),
                    "label_span": (label_lines[0][1], label_lines[-1][2]),
                    "amount": parse_amount(amount_row[0].strip()),
                    "amount_span": (amount_row[1], amount_row[2]),
                    "rating_text": " ".join(block),
                    "rating_span": (block_start, block_end),
                }
            label_lines, amount_row = [], None
            continue
        if NUMERIC_CELL.match(stripped):
            amount_row = (line, ls, le)
            i += 1
            continue
        label_lines.append((line, ls, le))
        i += 1


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """A CARE press release carries the facilities table, the liquidity
    paragraph and both directions of rating sensitivities."""
    return rating_rationale_coverage(AGENCY, claims, (
        (f"rating_sensitivity|{AGENCY}|positive", "positive rating sensitivities"),
        (f"rating_sensitivity|{AGENCY}|negative", "negative rating sensitivities"),
    ))


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    pub = doc_meta.published_date or find_published_date(pages[0]["text"])
    qkey = quarter_key(pub)

    for page in pages:
        pno, text = page["page_no"], page["text"]

        for row in _rows(text):
            grade = normalise_grade(row["rating_text"])
            if not grade:
                continue
            outlook = parse_outlook(row["rating_text"])
            watch = parse_watch(row["rating_text"])
            label = row["label"]
            slug = f"{label.lower()}|{row['amount']}"
            anchors = [anchor(pno, text, *row["label_span"]),
                       anchor(pno, text, *row["amount_span"]),
                       anchor(pno, text, *row["rating_span"])]

            claims.append(ExtractedClaim(
                claim_type="rating",
                fact_key=f"rating_instrument|{AGENCY}|{slug}|{qkey}",
                subject=f"{label} — {AGENCY} rating",
                value_text=row["rating_text"], as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION, anchors=anchors,
                rating=RatingDetail(agency=AGENCY, instrument=label,
                                    rated_amount_cr=row["amount"], rating=grade,
                                    instrument_class=rating_identity(label, grade)[0],
                                    term=rating_identity(label, grade)[1],
                                    outlook=outlook, watch=watch,
                                    action=parse_action(row["rating_text"]), action_date=pub),
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
                                    instrument_type=classify_instrument(label),
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

    return claims
