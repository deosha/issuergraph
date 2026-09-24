"""ICRA rating rationale extractor.

Handles three structures:
  * "Summary of rating action" / "Summary of rating(s) outstanding" table
  * "Annexure I: Instrument details" (ISIN-level, with maturity)
  * "Credit strengths" / "Credit challenges" bullet headlines
"""
from __future__ import annotations

import re

from ..models import DebtDetail, ExtractedClaim, RatingDetail
from . import history
from .coverage import Coverage, rating_rationale_coverage
from .common import (anchor, classify_instrument, find_published_date, lines_with_offsets,
                     normalise_grade, parse_action, parse_amount, parse_outlook,
                     parse_short_date, parse_watch, quarter_key, rating_identity)

EXTRACTOR = "icra_rationale"
VERSION = "2.3.0"

AGENCY = "ICRA"
SUMMARY_HEADS = ("Summary of rating action", "Summary of rating(s) outstanding")
SUMMARY_END = ("*Instrument details", "Rationale", "Key rating drivers")
RATING_LINE = re.compile(r"(PP-MLD)?\[ICRA\]")
NUMERIC_CELL = re.compile(r"^(?:[\d,]+\.\d{2}|-)$")
RATING_COMPLETE = re.compile(
    r"(Stable|Negative|Positive|Implications|reaffirmed|withdrawn|assigned|"
    r"outstanding|confirmed)$"
)
ISIN_CELL = re.compile(r"^(INE[A-Z0-9]{9}|Not placed|NA)$")
HEADER_CELL = re.compile(
    r"^(Instrument\*?|Previous rated|Current rated|Previous Rated|Current Rated|amount|Amount|"
    r"\(Rs\. crore\)|Rating [Aa]ction|Rating Outstanding|Rating outstanding)$"
)
LIQUIDITY_RE = re.compile(r"Liquidity position:\s*(?P<grade>[A-Za-z ]+)\n(?P<body>[^\n]+)")
SENSITIVITY_RE = re.compile(r"(?m)^(?P<kind>Positive|Negative) factors\s+–\s+(?P<body>[^\n]+)")
BULLET_HEAD = re.compile(r"(?m)^(?P<head>[A-Z][^\n]{14,159}?)\s+–\s+")
HISTORY_HEAD = "Rating history for past three years"
HISTORY_END = ("Complexity level",)
PAGE_FURNITURE = re.compile(r"www\.icra|^Page \| \d+$|^Sensitivity Label")


def _summary_block(text: str) -> tuple[int, int] | None:
    start = None
    for head in SUMMARY_HEADS:
        idx = text.find(head)
        if idx != -1:
            start = idx + len(head)
            break
    if start is None:
        return None
    end = len(text)
    for marker in SUMMARY_END:
        idx = text.find(marker, start)
        if idx != -1:
            end = min(end, idx)
    return start, end


def _parse_summary(page_no: int, text: str, span: tuple[int, int]):
    """Yield (label, label_span, prev_amt, curr_amt, curr_span, rating_text, rating_span)."""
    rows = list(lines_with_offsets(text, *span))
    i = 0
    label_lines: list[tuple[str, int, int]] = []
    while i < len(rows):
        line, ls, le = rows[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if NUMERIC_CELL.match(stripped) and i + 1 < len(rows) and NUMERIC_CELL.match(rows[i + 1][0].strip()):
            prev_tok, curr_tok = stripped, rows[i + 1][0].strip()
            curr_span = (rows[i + 1][1], rows[i + 1][2])
            j = i + 2
            if j >= len(rows) or not RATING_LINE.search(rows[j][0]):
                i += 1
                continue
            rating_start = rows[j][1]
            parts = []
            while j < len(rows):
                cell = rows[j][0].strip()
                parts.append(cell)
                rating_end = rows[j][2]
                j += 1
                nxt = rows[j][0].strip() if j < len(rows) else ""
                # A rating cell wraps across lines. The next row's label always
                # starts with a capital or digit; a wrapped continuation does not.
                if nxt and nxt[:1].islower():
                    continue
                if cell and not cell.endswith(",") and RATING_COMPLETE.search(cell):
                    break
            if not label_lines:
                i = j
                continue
            label = " ".join(l.strip() for l, _, _ in label_lines).strip()
            label_span = (label_lines[0][1], label_lines[-1][2])
            yield (label, label_span, parse_amount(prev_tok), parse_amount(curr_tok),
                   curr_span, " ".join(parts), (rating_start, rating_end))
            label_lines = []
            i = j
            continue
        if not HEADER_CELL.match(stripped):
            label_lines.append((line, ls, le))
        if len(label_lines) > 6:
            label_lines = label_lines[-6:]
        i += 1


def _annexure_rows(page_no: int, text: str):
    """Yield ISIN-level instrument rows from Annexure I."""
    rows = list(lines_with_offsets(text))
    i = 0
    while i < len(rows):
        cell = rows[i][0].strip()
        if not ISIN_CELL.match(cell):
            i += 1
            continue
        isin = cell
        name_lines = []
        j = i + 1
        while j < len(rows):
            nxt = rows[j][0].strip()
            if parse_short_date(nxt) or nxt == "NA":
                break
            if ISIN_CELL.match(nxt):
                break
            name_lines.append(rows[j])
            j += 1
        if not name_lines or j + 3 >= len(rows):
            i += 1
            continue
        issuance = rows[j][0].strip()
        coupon = rows[j + 1][0].strip()
        maturity_tok = rows[j + 2][0].strip()
        amount_tok = rows[j + 3][0].strip()
        amount = parse_amount(amount_tok)
        if amount is None:
            i = j + 1
            continue
        name = " ".join(l.strip() for l, _, _ in name_lines)
        yield {
            "isin": isin,
            "name": name,
            "name_span": (name_lines[0][1], name_lines[-1][2]),
            "issuance": parse_short_date(issuance),
            "coupon": coupon,
            "maturity": parse_short_date(maturity_tok),
            "maturity_span": (rows[j + 2][1], rows[j + 2][2]),
            "amount": amount,
            "amount_span": (rows[j + 3][1], rows[j + 3][2]),
            "row_span": (rows[i][1], rows[j + 3][2]),
        }
        i = j + 4


def _bullets(page_no: int, text: str, heading: str, stop: tuple[str, ...]):
    start = text.find(heading)
    if start == -1:
        return
    start += len(heading)
    end = len(text)
    for marker in stop:
        idx = text.find(marker, start)
        if idx != -1:
            end = min(end, idx)
    body = text[start:end]
    # Bullet headlines are the bold lead-in before an en dash.
    for m in BULLET_HEAD.finditer(body):
        head = m.group("head").replace("\n", " ").strip()
        if len(head.split()) < 3:
            continue
        yield head, (start + m.start("head"), start + m.end("head"))


# A material-event release (a watch placement after an RBI order, say) carries
# the summary table and a short rationale and refers the reader elsewhere for
# the rest: "liquidity position and rating sensitivities: Click here". Those
# sections are not missing from it; they were never in it.
MATERIAL_EVENT_MARKERS = ("Material event", "rating sensitivities: Click here")


def is_material_event_release(pages: list[dict]) -> bool:
    text = "\n".join(p["text"] for p in pages[:3])
    return all(marker in text for marker in MATERIAL_EVENT_MARKERS)


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """A full ICRA rationale carries the summary table, the liquidity position,
    both sensitivity directions and the strengths/challenges bullets. Each is
    required on its own. A material-event release declares itself to hold only
    the table, so only the table is required of it."""
    if is_material_event_release(pages):
        return rating_rationale_coverage(AGENCY, claims, (), liquidity=False)
    cov = rating_rationale_coverage(AGENCY, claims, (
        (f"rating_sensitivity|{AGENCY}|positive", "positive rating sensitivities"),
        (f"rating_sensitivity|{AGENCY}|negative", "negative rating sensitivities"),
        (f"rationale|{AGENCY}|strengths|", "credit strengths"),
        (f"rationale|{AGENCY}|weaknesses|", "credit challenges"),
    ))
    _, unreadable = _history(doc_meta, pages)
    return cov.merge(history.history_coverage(AGENCY, claims, unreadable))


def _history(doc_meta, pages: list[dict]):
    pub = getattr(doc_meta, "published_date", None) or find_published_date(pages[0]["text"])
    return history.read_annexure(AGENCY, EXTRACTOR, VERSION, pages, pub,
                                 HISTORY_HEAD, HISTORY_END, PAGE_FURNITURE)


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    """pages: [{'page_no': int, 'text': str}, ...] in order."""
    claims: list[ExtractedClaim] = []
    first = pages[0]["text"]
    pub = doc_meta.published_date or find_published_date(first)
    qkey = quarter_key(pub)

    for page in pages:
        pno, text = page["page_no"], page["text"]

        span = _summary_block(text)
        if span:
            for (label, label_span, prev_amt, curr_amt, curr_span,
                 rating_text, rating_span) in _parse_summary(pno, text, span):
                grade = normalise_grade(rating_text)
                if not grade:
                    continue
                outlook = parse_outlook(rating_text)
                watch = parse_watch(rating_text)
                anchors = [anchor(pno, text, *label_span), anchor(pno, text, *rating_span)]
                if curr_amt is not None:
                    anchors.insert(1, anchor(pno, text, *curr_span))
                # the same label repeats across tranches; amounts disambiguate
                slug = f"{label.lower()}|{prev_amt}|{curr_amt}"
                identity = rating_identity(label, grade)

                claims.append(ExtractedClaim(
                    claim_type="rating",
                    fact_key=f"rating_instrument|{AGENCY}|{slug}|{qkey}",
                    subject=f"{label} — {AGENCY} rating",
                    value_text=rating_text.strip(),
                    as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=anchors,
                    rating=RatingDetail(
                        agency=AGENCY, instrument=label, rated_amount_cr=curr_amt,
                        instrument_class=identity[0], term=identity[1],
                        rating=grade, outlook=outlook, watch=watch,
                        action=parse_action(rating_text), action_date=pub,
                    ),
                ))

                if curr_amt is not None:
                    claims.append(ExtractedClaim(
                        claim_type="debt_instrument",
                        fact_key=f"rated_amount|{AGENCY}|{slug}|{qkey}",
                        subject=f"{label} (rated amount)",
                        value_numeric=curr_amt, value_unit="INR_CRORE",
                        as_of_date=pub,
                        extractor=EXTRACTOR, extractor_version=VERSION,
                        anchors=[anchor(pno, text, *label_span), anchor(pno, text, *curr_span)],
                        debt=DebtDetail(
                            instrument_name=label,
                            instrument_type=classify_instrument(label),
                            amount_cr=curr_amt, as_of_date=pub,
                        ),
                    ))


        if "Annexure I: Instrument details" in text or "ISIN" in text[:400]:
            for row in _annexure_rows(pno, text):
                key = f"instrument|{row['isin']}|{row['maturity']}|{row['amount']}"
                claims.append(ExtractedClaim(
                    claim_type="debt_instrument",
                    fact_key=key,
                    subject=f"{row['name']} ({row['isin']})",
                    value_numeric=row["amount"], value_unit="INR_CRORE",
                    as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[anchor(pno, text, *row["row_span"])],
                    debt=DebtDetail(
                        instrument_name=f"{row['name']} {row['coupon']} ({row['isin']})".strip(),
                        instrument_type=classify_instrument(row["name"]),
                        amount_cr=row["amount"], maturity_date=row["maturity"],
                        as_of_date=pub,
                    ),
                ))

        for m in LIQUIDITY_RE.finditer(text):
            grade = m.group("grade").strip()
            claims.append(ExtractedClaim(
                claim_type="rationale_point",
                fact_key=f"liquidity_assessment|{AGENCY}",
                subject=f"{AGENCY} liquidity assessment",
                value_text=grade, as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("grade"), m.end("grade")),
                         anchor(pno, text, m.start("body"), m.end("body"))],
            ))
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_detail|{AGENCY}",
                subject=f"{AGENCY} liquidity narrative",
                value_text=m.group("body").strip(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("body"), m.end("body"))],
            ))

        for m in SENSITIVITY_RE.finditer(text):
            kind = m.group("kind").lower()
            claims.append(ExtractedClaim(
                claim_type="rationale_point",
                fact_key=f"rating_sensitivity|{AGENCY}|{kind}",
                subject=f"{AGENCY} {kind} rating factors",
                value_text=m.group("body").strip(), as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[anchor(pno, text, m.start("body"), m.end("body"))],
            ))

        for heading, section, stop in (
            ("Credit strengths", "strengths", ("Credit challenges", "Liquidity position")),
            ("Credit challenges", "weaknesses", ("Liquidity position", "Rating sensitivities",
                                                 "Environmental and social")),
        ):
            for head, hspan in _bullets(pno, text, heading, stop):
                claims.append(ExtractedClaim(
                    claim_type="rationale_point",
                    fact_key=f"rationale|{AGENCY}|{section}|{head.lower()}",
                    subject=f"{AGENCY} {section[:-1] if section.endswith('s') else section}",
                    value_text=head,
                    as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[anchor(pno, text, *hspan)],
                ))

    claims += _history(doc_meta, pages)[0]
    return claims
