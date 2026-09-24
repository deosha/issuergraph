"""Brickwork Ratings rationale extractor.

Also the only rationale in this corpus that publishes a headline "Total Debt"
figure, which is what makes it comparable to the annual report balance sheet.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import DebtDetail, ExtractedClaim, RatingDetail
from . import history
from .coverage import Coverage, rating_rationale_coverage
from .common import (anchor, classify_instrument, find_published_date, lines_with_offsets,
                     normalise_grade, parse_action, parse_amount, parse_outlook, parse_watch,
                     quarter_key, rating_identity)

EXTRACTOR = "bwr_rationale"
VERSION = "2.3.0"
AGENCY = "Brickwork"

TENURE_CELL = re.compile(r"^(Long Term|Short Term)$")
HEADER_DATE = re.compile(r"^\([^()]*\d{4}\)$")                     # "(30th Sept 2024)"
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
HISTORY_HEAD = "Rating History for the past 3 years"
# The table ends at its Total row or the footnote beneath it.
HISTORY_END = ("\nTotal\n", "^Public Issue", "ANNEXURE I")

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


def segments(page: dict, begin: int, end: int, gap: float = 6.0) -> list[dict]:
    """Cells of a table region, from word boxes: consecutive words on one line
    closer than `gap` points. PyMuPDF can put two cells on one line ("NCDs
    Public Issue ** 975.82" in Dec 2025); a cell is still an exact substring
    of the page text, so its offsets anchor directly."""
    text = page["text"]
    words = sorted((w for w in page["word_map"] if begin <= w[0] < end), key=lambda w: w[0])
    out: list[dict] = []
    cur = None
    for s0, e0, x0, y0, x1, y1 in words:
        same_line = cur is not None and "\n" not in text[cur["end"]:int(s0)]
        if same_line and x0 - cur["x1"] <= gap:
            cur["end"], cur["x1"] = int(e0), x1
        else:
            cur = {"start": int(s0), "end": int(e0), "x0": x0, "x1": x1, "y": (y0 + y1) / 2}
            out.append(cur)
    for c in out:
        c["text"] = text[c["start"]:c["end"]]
    return out


def _centre(seg: dict) -> float:
    return (seg["x0"] + seg["x1"]) / 2


def _particulars_rows(page: dict):
    """Rows of the Particulars table, by column position.

    Columns come from the header: Instrument(s), Amount Previous / Present,
    Tenure, Rating Previous / Present. Each row is built around its one tenure
    cell ("Long Term"), and every other cell joins the row whose tenure cell is
    nearest vertically — labels and ratings wrap over several lines.
    """
    text = page["text"]
    start = text.find("Particulars:")
    if start == -1:
        return
    end = text.find("*Please refer", start)
    cells = segments(page, start, end if end != -1 else len(text))
    label_head = next((c for c in cells if re.match(r"^Instruments?$", c["text"])), None)
    tenure_head = next((c for c in cells if c["text"] == "Tenure"), None)
    prev = [c for c in cells if c["text"] == "Previous"]
    pres = [c for c in cells if c["text"] == "Present"]
    if not (label_head and tenure_head and len(prev) == 2 and len(pres) == 2):
        return
    columns = {"label": _centre(label_head), "amount_prev": _centre(prev[0]),
               "amount": _centre(pres[0]), "tenure": _centre(tenure_head),
               "rating_prev": _centre(prev[1]), "rating": _centre(pres[1])}
    all_tenures = [c for c in cells if TENURE_CELL.match(c["text"])]
    if not all_tenures:
        return
    # The header ends above the first row; it includes the previous-rating
    # column's date ("(30th Sept 2024)", "(21st Nov 2025)"). The body ends above
    # the Total line and the amount in words beneath the last row.
    top = min(c["y"] for c in all_tenures) - 12
    header_bottom = max([c["y"] for c in [label_head, tenure_head, *prev, *pres]]
                        + [c["y"] for c in cells if HEADER_DATE.match(c["text"])
                           and c["y"] < top])
    enders = [c["y"] for c in cells if c["text"].startswith(("Total", "Rupees"))]
    bottom = min(enders) - 3 if enders else float("inf")
    body = [c for c in cells if header_bottom < c["y"] < bottom]
    tenures = [c for c in body if TENURE_CELL.match(c["text"])]
    rows = [{k: [] for k in columns} for _ in tenures]

    def nearest_row(y: float) -> int:
        return min(range(len(tenures)), key=lambda i: abs(tenures[i]["y"] - y))

    by_column: dict[str, list[dict]] = {k: [] for k in columns}
    for c in body:
        if c in tenures:
            continue
        column = min(columns, key=lambda k: abs(columns[k] - _centre(c)))
        if column == "label" and c["x0"] > columns["amount_prev"]:
            continue
        by_column[column].append(c)
    for column, found in by_column.items():
        found.sort(key=lambda c: (c["y"], c["x0"]))
        if column in ("rating", "rating_prev"):
            # A rating cell can be taller than its row ("Reaffirmed and /
            # removed Rating Watch / with Negative / Implications"): it is a
            # block from a "BWR" or "-" line down to the next one, placed by
            # its first line.
            blocks: list[list[dict]] = []
            for c in found:
                if BWR_CELL.match(c["text"]) or c["text"] == "-" or not blocks:
                    blocks.append([c])
                else:
                    blocks[-1].append(c)
            for block in blocks:
                rows[nearest_row(block[0]["y"])][column].extend(block)
        else:
            for c in found:
                rows[nearest_row(c["y"])][column].append(c)
    for tenure, row in zip(tenures, rows):
        for column in row.values():
            column.sort(key=lambda c: (c["y"], c["x0"]))
        rating = [c for c in row["rating"] if c["text"] != "-"]
        amount = row["amount"][-1] if row["amount"] else None
        if not row["label"] or not rating or amount is None:
            continue
        previous = " ".join(c["text"] for c in row["rating_prev"] if c["text"] != "-")
        yield {
            "label": re.sub(r"\s*[*^#]+\s*$", "",
                            " ".join(c["text"] for c in row["label"])).strip(),
            "label_cells": row["label"], "amount": parse_amount(amount["text"]),
            "amount_cell": amount, "rating_text": " ".join(c["text"] for c in rating),
            "rating_cells": rating, "previous_text": previous or None,
        }


def _action(text: str) -> str | None:
    found = parse_action(text)
    if found is None and "assignment" in text.lower():
        return "assigned"
    return found


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """A Brickwork rationale carries the particulars table, the liquidity
    paragraph, the strengths/risks bullets and the financials table from
    which Total Debt is read."""
    cov = rating_rationale_coverage(AGENCY, claims, (
        (f"rationale|{AGENCY}|strengths|", "credit strengths"),
        (f"rationale|{AGENCY}|weaknesses|", "credit risks"),
        ("total_borrowings|", "Total Debt row in the financials table"),
    ))
    _, unreadable = _history(doc_meta, pages)
    return cov.merge(history.history_coverage(AGENCY, claims, unreadable))


def _history(doc_meta, pages: list[dict]):
    """The rating-history annexure, in whichever of Brickwork's layouts it uses.

    Up to Sep 2025 each column header is a date ("30 Sept 2024") and the shared
    reader handles it. From Nov 2025 the columns are the current rating, the
    previous action and then calendar years, and each entry carries its own
    date after the rating text; that layout is read by _history_dated_cells.
    """
    pub = getattr(doc_meta, "published_date", None) or find_published_date(pages[0]["text"])
    claims, unreadable = history.read_annexure(AGENCY, EXTRACTOR, VERSION, pages, pub,
                                               HISTORY_HEAD, HISTORY_END)
    if claims or unreadable:
        return claims, unreadable
    entries, unreadable = _history_dated_cells(pages)
    return history.history_claims(AGENCY, EXTRACTOR, VERSION, pages, entries, pub), unreadable


MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
ENTRY_DATE = re.compile(rf"\(?(?P<d>\d{{1,2}})\s*(?P<m>{MONTH})\s*(?P<y>\d{{4}})\)?"
                        r"|\((?P<dd>\d{2})\.(?P<mm>\d{2})\.(?P<yy>\d{4})\)")
from decimal import Decimal  # noqa: E402  (used by the dated-cells reader)

COLUMN_LABEL = re.compile(rf"^(?:20\d\d|Rating|\((?:\d{{1,2}}\s*)?{MONTH}\s*\d{{4}}\)?|"
                          rf"\(\d{{1,2}}\s*{MONTH}|\d{{4}}\))$")


def _entry_date(m) -> date | None:
    if m.group("d"):
        return history.month_day_year(m.group("m"), m.group("d"), m.group("y"))
    return date(int(m.group("yy")), int(m.group("mm")), int(m.group("dd")))


def _history_dated_cells(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """Entries of the Nov-2025 layout: rating text followed by its own date.

    Rows are anchored on the amount column — one amount per row, printed even
    where the serial number is merged into the name ("3 Secured") or missing
    after a page break — and the last amount, equal to the sum of the others,
    is the table's total. Columns come from the header labels (the current
    rating, the previous action, then years). Each column is read top to bottom
    across the whole table and split at each "BWR" or "Withdrawn"; an entry's
    date is the one that follows it, else its column header's date ("(15 Sept
    2025)"), and the entry belongs to the row whose amount is nearest its first
    line — ratings are taller than rows. The current-rating column is this
    document's own action, not history. A "Withdrawn" under a year with no date
    anywhere cannot be dated and is returned as undated, not guessed.
    """
    header, body = history.table_lines(pages, HISTORY_HEAD, HISTORY_END)
    lines = header + body
    amount_head = next((l for l in lines if l["text"].startswith(("Amount", "Type Amount"))),
                       None)
    if not lines or amount_head is None:
        return [], []
    amounts = [l for l in lines if re.fullmatch(r"[\d,]+\.\d{2}", l["text"])
               and amount_head["x0"] - 10 < l["x0"] < amount_head["x1"] + 10]
    running = Decimal(0)
    rows_at, total = [], None
    for a in amounts:
        value = parse_amount(a["text"])
        if rows_at and value is not None and abs(value - running) < Decimal("0.02"):
            total = a                               # the Total row: the sum of the rest
            break
        rows_at.append(a)
        running += value or 0
    if not rows_at:
        return [], []
    first_row = rows_at[0]
    labels = [l for l in lines if l["x0"] > amount_head["x1"] - 5
              and (l["page_no"], l["y"]) < (first_row["page_no"], first_row["y"] - 30)
              and COLUMN_LABEL.match(l["text"])]
    columns: list[dict] = []
    for l in sorted(labels, key=lambda l: (l["x0"] + l["x1"]) / 2):
        centre = (l["x0"] + l["x1"]) / 2
        if columns and abs(columns[-1]["centre"] - centre) < 25:
            columns[-1]["labels"].append(l)
        else:
            columns.append({"centre": centre, "labels": [l]})
    if not columns:
        return [], []
    for n, col in enumerate(columns):
        text = " ".join(l["text"] for l in sorted(col["labels"], key=lambda l: l["y"]))
        m = ENTRY_DATE.search(text)
        col["date"] = _entry_date(m) if m and m.group("d") else None
        col["current"] = n == 0

    # The header ends, in reading order, at the last column label: a rating
    # cell taller than its row can start higher on the page than the row's own
    # amount, so no y cut-off separates header from body.
    header_end = max((l["page_no"], l["start"]) for l in labels + [amount_head])

    def in_body(l: dict) -> bool:
        before_total = total is None or (l["page_no"], l["start"]) < (total["page_no"],
                                                                      total["start"])
        return (l["page_no"], l["start"]) > header_end and before_total

    # Rows by reading order, not position. The content stream draws a table row
    # by row — a row's serial, name and tenure, then its amount, then its
    # rating cells — while the cells themselves are taller than the row and
    # centred on it, so no vertical rule separates stacked entries of adjacent
    # rows. A cell belongs to the last amount before it; a name line to the
    # next amount.
    rows = [{"label": [], "name": "", "cells": []} for _ in rows_at]
    pending: list[dict] = []
    current = -1
    for l in sorted(lines, key=lambda l: (l["page_no"], l["start"])):
        if l in rows_at:
            current = rows_at.index(l)
            rows[current]["label"].extend(pending)
            pending = []
            continue
        if l in labels or l is amount_head or not in_body(l):
            continue
        if l["x0"] < amount_head["x0"] - 5:
            if not re.fullmatch(r"\d{1,2}|(?:Long|Short)(?: Term)?|Term", l["text"]):
                pending.append(l)
        elif l["x0"] > amount_head["x1"] - 5 and current >= 0:
            rows[current]["cells"].append(l)
    for row in rows:
        row["label"].sort(key=lambda l: (l["page_no"], l["y"]))
        name = " ".join(l["text"] for l in row["label"])
        name = re.sub(r"^\d{1,2}\s+", "", name)                         # "3 Secured"
        name = re.sub(r"\s+(?:Long|Short)(?:\s+Term)?\b", "", name)       # "NCDs ^ Long"
        row["name"] = re.sub(r"\s*[*^#]+", "", name).strip()

    entries, unreadable, undated = [], [], []
    for row in rows:
        for col in columns:
            if col["current"]:
                continue
            mine = sorted((l for l in row["cells"] if min(
                columns, key=lambda c: abs(c["centre"] - (l["x0"] + l["x1"]) / 2)) is col),
                key=lambda l: (l["page_no"], l["y"]))
            spans, parts, pos = [], [], 0
            for l in mine:
                spans.append((pos, pos + len(l["text"]), l))
                parts.append(l["text"])
                pos += len(l["text"]) + 1
            text = " ".join(parts)
            cuts = sorted({m.start() for m in re.finditer(r"BWR|Withdrawn", text)} | {len(text)})
            for a, b in zip(cuts, cuts[1:]):
                piece = text[a:b]
                dm = None
                for dm in ENTRY_DATE.finditer(piece):
                    pass
                when = _entry_date(dm) if dm else col["date"]
                used = [l for s0, e0, l in spans if e0 > a and s0 < b]
                read = history.read_rating(piece)
                if when is None and read["withdrawn"] and not read["grade"]:
                    undated.append(f"{row['name']!r}: {piece.strip()!r} (year column only)")
                    continue
                if when is None or not (read["grade"] or read["withdrawn"]):
                    unreadable.append(f"{row['name']!r} {piece[:60]!r}")
                    continue
                entries.append({"row": row, "date": when,
                                "date_lines": [] if dm else col["labels"],
                                "rating_lines": used, "rating_text": piece.strip(), **read})
    _history_dated_cells.undated = undated
    return entries, unreadable


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    pub = doc_meta.published_date or find_published_date(pages[0]["text"])
    qkey = quarter_key(pub)

    for page in pages:
        pno, text = page["page_no"], page["text"]

        if "Particulars:" in text:
            for row in _particulars_rows(page):
                grade = normalise_grade(row["rating_text"])
                if not grade or row["amount"] is None:
                    continue
                outlook = parse_outlook(row["rating_text"])
                label = row["label"]
                slug = f"{label.lower()}|{row['amount']}"
                anchors = ([anchor(pno, text, c["start"], c["end"]) for c in row["label_cells"]]
                           + [anchor(pno, text, row["amount_cell"]["start"],
                                     row["amount_cell"]["end"])]
                           + [anchor(pno, text, c["start"], c["end"])
                              for c in row["rating_cells"]])

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
                                        outlook=outlook, watch=parse_watch(row["rating_text"]),
                                        action=_action(row["rating_text"]),
                                        previous_rating=row["previous_text"], action_date=pub),
                ))
                claims.append(ExtractedClaim(
                    claim_type="debt_instrument",
                    fact_key=f"rated_amount|{AGENCY}|{slug}|{qkey}",
                    subject=f"{label} (rated amount)",
                    value_numeric=row["amount"], value_unit="INR_CRORE", as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=anchors[:len(row["label_cells"]) + 1],
                    debt=DebtDetail(instrument_name=label,
                                    instrument_type=classify_instrument(label),
                                    amount_cr=row["amount"], as_of_date=pub),
                ))


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

    claims += _history(doc_meta, pages)[0]
    return claims
