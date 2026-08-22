"""Brickwork Ratings rationale extractor.

Also the only rationale in this corpus that publishes a headline "Total Debt"
figure, which is what makes it comparable to the annual report balance sheet.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import DebtDetail, ExtractedClaim, RatingDetail
from .common import (anchor, classify_instrument, find_published_date, lines_with_offsets,
                     normalise_grade, parse_action, parse_amount, parse_outlook, parse_watch,
                     quarter_key)

EXTRACTOR = "bwr_rationale"
VERSION = "1.0.0"
AGENCY = "Brickwork"

TENURE_CELL = re.compile(r"^(Long Term|Short Term)$")
NUMERIC_CELL = re.compile(r"^(?:[\d,]+(?:\.\d+)?|-)$")
BWR_CELL = re.compile(r"^BWR\s+[A-D]")
HEADER_CELL = re.compile(r"^(Instrument|Amount \(Rs Crs\)|Tenure|Rating|Previous|Present|"
                         r"Particulars:|\(30th Sept \d{4}\))$")
FIN_TABLE_RE = re.compile(
    r"Key Financial Performance - IIFL Finance Ltd\.\s*\((?P<basis>Consolidated|Standalone)\)"
)
TOTAL_DEBT_RE = re.compile(r"(?m)^Total Debt\nRs\.in Crores\n(?P<row>[\d,]+(?:\n[\d,]+){0,5})")
PERIOD_RE = re.compile(r"(?m)^(?P<periods>FY\d{2}(?:\n(?:FY\d{2}|Q\dFY\d{2}))+)$")
BULLET_RE = re.compile(r"(?m)^●​?\s*(?P<head>[^:\n]{10,140}):\s*(?P<body>[^\n]*)")
LIQUIDITY_RE = re.compile(r"LIQUIDITY POSITION:\s*(?P<grade>[A-Z ]+)\n(?P<body>[^\n]+)")

FY_END = {"FY22": date(2022, 3, 31), "FY23": date(2023, 3, 31), "FY24": date(2024, 3, 31),
          "FY25": date(2025, 3, 31), "FY26": date(2026, 3, 31)}
QTR_END = {"Q1": (6, 30), "Q2": (9, 30), "Q3": (12, 31), "Q4": (3, 31)}


def _period_to_date(token: str) -> date | None:
    token = token.strip()
    if token in FY_END:
        return FY_END[token]
    m = re.match(r"^(Q\d)(FY\d{2})$", token)
    if m and m.group(2) in FY_END:
        month, day = QTR_END[m.group(1)]
        year = FY_END[m.group(2)].year - (1 if month > 3 else 0)
        return date(year, month, day)
    return None


def _starts_new_row(rows: list[tuple[str, int, int]], i: int) -> bool:
    """True if rows[i] begins the next table row: label lines, amounts, then a tenure cell."""
    labels = 0
    j = i
    while j < len(rows) and labels <= 4:
        cell = rows[j][0].strip()
        if not cell:
            j += 1
            continue
        if NUMERIC_CELL.match(cell):
            break
        if BWR_CELL.match(cell) or TENURE_CELL.match(cell):
            return False
        labels += 1
        j += 1
    amounts = 0
    while j < len(rows) and amounts <= 2:
        cell = rows[j][0].strip()
        if not cell:
            j += 1
            continue
        if NUMERIC_CELL.match(cell):
            amounts += 1
            j += 1
            continue
        return bool(amounts) and bool(TENURE_CELL.match(cell))
    return False


def _particulars_rows(text: str):
    start = text.find("Particulars:")
    end = text.find("*Please refer", start if start != -1 else 0)
    rows = list(lines_with_offsets(text, max(start, 0), end if end != -1 else None))
    label_lines: list[tuple[str, int, int]] = []
    amounts: list[tuple[str, int, int]] = []
    i = 0
    while i < len(rows):
        line, ls, le = rows[i]
        stripped = line.strip()
        if not stripped or HEADER_CELL.match(stripped):
            i += 1
            continue
        if TENURE_CELL.match(stripped) and label_lines and amounts:
            i += 1
            groups: list[tuple[list[str], int, int]] = []
            while i < len(rows):
                cell = rows[i][0].strip()
                if cell == "-" and not groups:
                    i += 1          # empty "previous rating" cell
                    continue
                if TENURE_CELL.match(cell) or NUMERIC_CELL.match(cell):
                    break
                # a parenthetical is always a continuation of the rating cell above it
                if groups and not cell.startswith("(") and _starts_new_row(rows, i):
                    break
                if BWR_CELL.match(cell):
                    groups.append(([cell], rows[i][1], rows[i][2]))
                elif groups and cell and cell != "-":
                    groups[-1][0].append(cell)
                    groups[-1] = (groups[-1][0], groups[-1][1], rows[i][2])
                i += 1
                if cell.startswith("Total"):
                    break
            if groups:
                present_cells, pstart, pend = groups[-1]
                yield {
                    "label": " ".join(l.strip() for l, _, _ in label_lines),
                    "label_span": (label_lines[0][1], label_lines[-1][2]),
                    "amount": parse_amount(amounts[-1][0].strip()),
                    "amount_span": (amounts[-1][1], amounts[-1][2]),
                    "rating_text": " ".join(present_cells),
                    "rating_span": (pstart, pend),
                    "previous_text": " ".join(groups[0][0]) if len(groups) > 1 else None,
                }
            label_lines, amounts = [], []
            continue
        if NUMERIC_CELL.match(stripped):
            amounts.append((line, ls, le))
            i += 1
            continue
        if not amounts:
            label_lines.append((line, ls, le))
        else:
            label_lines, amounts = [(line, ls, le)], []
        i += 1


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    pub = doc_meta.published_date or find_published_date(pages[0]["text"])
    qkey = quarter_key(pub)
    issuer_emitted = False

    for page in pages:
        pno, text = page["page_no"], page["text"]

        if "Particulars:" in text:
            for row in _particulars_rows(text):
                grade = normalise_grade(row["rating_text"])
                if not grade or row["amount"] is None:
                    continue
                outlook = parse_outlook(row["rating_text"])
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
                                        outlook=outlook, watch=parse_watch(row["rating_text"]),
                                        action=parse_action(row["rating_text"]),
                                        previous_rating=row["previous_text"], action_date=pub),
                ))
                claims.append(ExtractedClaim(
                    claim_type="debt_instrument",
                    fact_key=f"rated_amount|{AGENCY}|{slug}|{qkey}",
                    subject=f"{label} (rated amount)",
                    value_numeric=row["amount"], value_unit="INR_CRORE", as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION, anchors=anchors[:2],
                    debt=DebtDetail(instrument_name=label,
                                    instrument_type=classify_instrument(label),
                                    amount_cr=row["amount"], as_of_date=pub),
                ))

                if not issuer_emitted:
                    issuer_emitted = True
                    ranchor = anchor(pno, text, *row["rating_span"])
                    claims.append(ExtractedClaim(
                        claim_type="rating", fact_key=f"rating_grade|long_term|{qkey}",
                        subject="Long-term issuer rating (grade)", value_text=grade,
                        as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                        anchors=[ranchor],
                        rating=RatingDetail(agency=AGENCY, instrument=label, rating=grade,
                                            outlook=outlook, action=parse_action(row["rating_text"]),
                                            action_date=pub)))
                    if outlook:
                        claims.append(ExtractedClaim(
                            claim_type="rating", fact_key=f"rating_outlook|long_term|{qkey}",
                            subject="Long-term rating outlook", value_text=outlook,
                            as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                            anchors=[ranchor]))

        basis_match = FIN_TABLE_RE.search(text)
        if basis_match:
            basis = basis_match.group("basis").lower()
            periods_match = PERIOD_RE.search(text, basis_match.end())
            debt_match = TOTAL_DEBT_RE.search(text, basis_match.end())
            if periods_match and debt_match:
                periods = periods_match.group("periods").split("\n")
                values = debt_match.group("row").split("\n")
                offset = debt_match.start("row")
                for idx, token in enumerate(values):
                    if idx >= len(periods):
                        break
                    as_of = _period_to_date(periods[idx])
                    amount = parse_amount(token)
                    if amount is None or as_of is None:
                        offset += len(token) + 1
                        continue
                    claims.append(ExtractedClaim(
                        claim_type="total_borrowings",
                        fact_key=f"total_borrowings|{basis}|{as_of.isoformat()}",
                        subject=f"Total debt ({basis}, {periods[idx]})",
                        value_numeric=amount, value_unit="INR_CRORE", basis=basis,
                        as_of_date=as_of, extractor=EXTRACTOR, extractor_version=VERSION,
                        anchors=[anchor(pno, text, offset, offset + len(token)),
                                 anchor(pno, text, basis_match.start(), basis_match.end())],
                        debt=DebtDetail(instrument_name=f"Total debt ({basis})",
                                        instrument_type="total", amount_cr=amount,
                                        as_of_date=as_of),
                    ))
                    offset += len(token) + 1

        for m in BULLET_RE.finditer(text):
            head = m.group("head").strip()
            section = "strengths" if text.rfind("Credit Strengths", 0, m.start()) > \
                text.rfind("Credit Risks", 0, m.start()) else "weaknesses"
            if "Credit Strengths" not in text and "Credit Risks" not in text:
                continue
            claims.append(ExtractedClaim(
                claim_type="rationale_point",
                fact_key=f"rationale|{AGENCY}|{section}|{head.lower()}",
                subject=f"{AGENCY} {section[:-1]}", value_text=head, as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("head"), m.end("head"))]))

        for m in LIQUIDITY_RE.finditer(text):
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_assessment|{AGENCY}",
                subject=f"{AGENCY} liquidity assessment",
                value_text=m.group("grade").strip().title(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("grade"), m.end("grade")),
                         anchor(pno, text, m.start("body"), m.end("body"))]))
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_detail|{AGENCY}",
                subject=f"{AGENCY} liquidity narrative",
                value_text=m.group("body").strip(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("body"), m.end("body"))]))

    return claims
