"""Annual report balance-sheet extractor.

Ind AS NBFC balance sheets do not print a "total borrowings" line. The figure
analysts quote is the sum of three line items, so this extractor emits the three
components as their own claims *and* a computed total whose evidence is all
three source rows. The arithmetic is stated in the claim's subject; the anchors
let a reader re-add it themselves.

Two things here are deliberately not assumed:

*Presence.* Locating a balance sheet and parsing one are separate steps. A page
that announces itself as a balance sheet but whose rows do not parse is a
failure, reported through `check_coverage`, not a page to skip quietly. The
earlier version returned `[]` for an unparseable statement, which is
indistinguishable from an issuer that reports no borrowings.

*Scale.* The unit comes from the statement's own "(` in Crores)" declaration and
is anchored alongside every amount, so a fact's citation proves its magnitude as
well as its digits. A statement in lakhs is converted, and the conversion is
stated in the claim's subject.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from ..models import DebtDetail, ExtractedClaim
from .common import anchor, classify_instrument
from .coverage import Coverage
from .units import CANONICAL, UnknownUnit, find_unit

EXTRACTOR = "annual_report_balance_sheet"
VERSION = "2.0.0"

# The heading locates the statement; case and spacing vary between report years,
# so the locator is deliberately looser than the parser it feeds.
BASIS_RE = re.compile(r"(?im)^\s*(CONSOLIDATED|STANDALONE)\s+BALANCE\s+SHEET\b")
COLUMN_DATE_RE = re.compile(r"As at\s*\n\s*March\s+(?P<day>\d{2}),\s*(?P<year>\d{4})",
                            re.IGNORECASE)
COMPONENTS = (
    "Debt securities",
    "Borrowings (other than debt securities)",
    "Subordinated liabilities",
)
REQUIRED_BASES = ("consolidated", "standalone")
COLUMNS_PER_STATEMENT = 2          # current year and its comparative


def _component(text: str, label: str):
    """Return the label span and both comparative amounts for one row."""
    pattern = re.compile(
        r"(?im)^" + re.escape(label) + r"\s*\n(?:\d+(?:\.\d+)?\n)?"
        r"(?P<a>[\d,]+\.\d{2})\s*\n(?P<b>[\d,]+\.\d{2})"
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


def _statements(pages: list[dict]):
    """Every page that announces itself as a balance sheet, with its basis."""
    for page in pages:
        match = BASIS_RE.search(page["text"])
        if match:
            yield page, match.group(1).lower(), match.start()


def _parse_statement(page: dict, basis: str, heading_at: int):
    """Parse one located statement. Returns (claims, problems)."""
    pno, text = page["page_no"], page["text"]
    where = f"{basis} balance sheet on page {pno}"
    problems: list[str] = []

    try:
        unit = find_unit(text, before=heading_at + 4000)
    except UnknownUnit as exc:
        return [], [f"{where}: {exc}"]
    if unit is None:
        return [], [f"{where}: no monetary unit declared — "
                    f"cannot establish whether amounts are crores or lakhs"]

    columns = [date(int(m.group("year")), 3, int(m.group("day")))
               for m in COLUMN_DATE_RE.finditer(text)][:COLUMNS_PER_STATEMENT]
    if len(columns) < COLUMNS_PER_STATEMENT:
        return [], [f"{where}: found {len(columns)} column date(s), "
                    f"expected {COLUMNS_PER_STATEMENT}"]

    rows = {label: _component(text, label) for label in COMPONENTS}
    missing = [label for label, row in rows.items() if row is None]
    if missing:
        # Every row is needed: a total summed from two of three components is
        # wrong in a way that still looks plausible, so emit nothing and say so.
        return [], [f"{where}: row(s) not found: {', '.join(missing)}"]

    unit_anchor = anchor(pno, text, *unit.span)
    scale_note = "" if unit.is_canonical else (
        f", converted from {unit.scale} at x{unit.factor}")
    claims: list[ExtractedClaim] = []

    for col_idx, as_of in enumerate(columns):
        total = Decimal(0)
        total_anchors = []
        for label in COMPONENTS:
            raw, span = rows[label]["values"][col_idx]
            amount = unit.to_crore(Decimal(raw.replace(",", "")))
            total += amount
            total_anchors.append(anchor(pno, text, *span))

            claims.append(ExtractedClaim(
                claim_type="debt_instrument",
                fact_key=f"balance_sheet|{basis}|{label.lower()}|{as_of.isoformat()}",
                subject=f"{label} ({basis}, as at {as_of.isoformat()}){scale_note}",
                value_numeric=f"{amount:.2f}", value_unit=CANONICAL, basis=basis,
                as_of_date=as_of, extractor=EXTRACTOR, extractor_version=VERSION,
                # the unit anchor rides with every amount: the citation proves
                # the scale, not just the digits
                anchors=[anchor(pno, text, *rows[label]["label_span"]),
                         anchor(pno, text, *span), unit_anchor],
                debt=DebtDetail(instrument_name=label,
                                instrument_type=classify_instrument(label),
                                amount_cr=f"{amount:.2f}", as_of_date=as_of),
            ))

        claims.append(ExtractedClaim(
            claim_type="total_borrowings",
            fact_key=f"total_borrowings|{basis}|{as_of.isoformat()}",
            subject=(f"Total borrowings ({basis}, as at {as_of.isoformat()}) = "
                     "debt securities + borrowings (other than debt securities) "
                     f"+ subordinated liabilities{scale_note}"),
            value_numeric=f"{total:.2f}", value_unit=CANONICAL, basis=basis,
            as_of_date=as_of, extractor=EXTRACTOR, extractor_version=VERSION,
            anchors=[*total_anchors, unit_anchor],
            debt=DebtDetail(instrument_name=f"Total borrowings ({basis})",
                            instrument_type="total", amount_cr=f"{total:.2f}",
                            as_of_date=as_of),
        ))

    return claims, problems


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    for page, basis, heading_at in _statements(pages):
        found, _ = _parse_statement(page, basis, heading_at)
        claims.extend(found)
    return claims


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """An annual report must yield both statements, in full.

    Both bases, both comparative columns, three components and a total each:
    sixteen facts. Anything less is reported as incomplete with the reason,
    which is what makes a layout change visible instead of merely quiet.
    """
    cov = Coverage()
    located = {basis: (page, at) for page, basis, at in _statements(pages)}

    for basis in REQUIRED_BASES:
        if not cov.require(basis in located,
                           f"no {basis} balance sheet found in this report"):
            continue
        page, heading_at = located[basis]
        _, problems = _parse_statement(page, basis, heading_at)
        cov.require(not problems, "; ".join(problems))

        expected = COLUMNS_PER_STATEMENT * (len(COMPONENTS) + 1)
        actual = len([c for c in claims if c.basis == basis])
        cov.require(
            actual == expected,
            f"{basis}: {actual} of {expected} expected facts "
            f"({len(COMPONENTS)} components + total, over "
            f"{COLUMNS_PER_STATEMENT} comparative columns)",
        )

    cov.require(
        all(c.value_unit == CANONICAL for c in claims),
        "some amounts are not expressed in " + CANONICAL,
    )
    return cov
