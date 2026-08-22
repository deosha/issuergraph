"""Annual report balance-sheet extractor.

Ind AS NBFC balance sheets do not print a "total borrowings" line. The figure
analysts quote is the sum of three line items, so this extractor emits the three
components as their own claims *and* a computed total whose evidence is all
three source rows. The arithmetic is stated in the claim's subject; the anchors
let a reader re-add it themselves.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import DebtDetail, ExtractedClaim
from .common import anchor, classify_instrument

EXTRACTOR = "annual_report_balance_sheet"
VERSION = "1.0.0"

BASIS_RE = re.compile(r"(?m)^(CONSOLIDATED|STANDALONE) BALANCE SHEET")
COLUMN_DATE_RE = re.compile(r"As at\nMarch (?P<day>\d{2}), (?P<year>\d{4})")
COMPONENTS = (
    "Debt securities",
    "Borrowings (other than debt securities)",
    "Subordinated liabilities",
)


def _component(text: str, label: str):
    """Return (amounts, spans) for a balance-sheet row: two comparative columns."""
    pattern = re.compile(
        r"(?m)^" + re.escape(label) + r"\n(?:\d+(?:\.\d+)?\n)?"
        r"(?P<a>[\d,]+\.\d{2})\n(?P<b>[\d,]+\.\d{2})"
    )
    m = pattern.search(text)
    if not m:
        return None
    return {
        "label_span": (m.start(), m.start() + len(label)),
        "values": [
            (m.group("a"), (m.start("a"), m.end("a"))),
            (m.group("b"), (m.start("b"), m.end("b"))),
        ],
    }


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []

    for page in pages:
        pno, text = page["page_no"], page["text"]
        basis_match = BASIS_RE.search(text)
        if not basis_match:
            continue
        basis = basis_match.group(1).lower()

        columns = [date(int(m.group("year")), 3, int(m.group("day")))
                   for m in COLUMN_DATE_RE.finditer(text)][:2]
        if len(columns) < 2:
            continue

        rows = {label: _component(text, label) for label in COMPONENTS}
        if any(row is None for row in rows.values()):
            continue

        for col_idx, as_of in enumerate(columns):
            total = 0
            total_anchors = []
            for label in COMPONENTS:
                raw, span = rows[label]["values"][col_idx]
                amount = raw.replace(",", "")
                total += float(amount)
                total_anchors.append(anchor(pno, text, *span))

                claims.append(ExtractedClaim(
                    claim_type="debt_instrument",
                    fact_key=f"balance_sheet|{basis}|{label.lower()}|{as_of.isoformat()}",
                    subject=f"{label} ({basis}, as at {as_of.isoformat()})",
                    value_numeric=amount, value_unit="INR_CRORE", basis=basis, as_of_date=as_of,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[anchor(pno, text, *rows[label]["label_span"]),
                             anchor(pno, text, *span)],
                    debt=DebtDetail(instrument_name=label,
                                    instrument_type=classify_instrument(label),
                                    amount_cr=amount, as_of_date=as_of),
                ))

            claims.append(ExtractedClaim(
                claim_type="total_borrowings",
                fact_key=f"total_borrowings|{basis}|{as_of.isoformat()}",
                subject=(f"Total borrowings ({basis}, as at {as_of.isoformat()}) = "
                         "debt securities + borrowings (other than debt securities) "
                         "+ subordinated liabilities"),
                value_numeric=f"{total:.2f}", value_unit="INR_CRORE", basis=basis,
                as_of_date=as_of, extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=total_anchors,
                debt=DebtDetail(instrument_name=f"Total borrowings ({basis})",
                                instrument_type="total", amount_cr=f"{total:.2f}",
                                as_of_date=as_of),
            ))

    return claims
