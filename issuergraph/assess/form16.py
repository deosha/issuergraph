"""Form 16 Part B (TRACES layout): salary and tax figures certified by the employer.

Deterministic: each line is found by its printed label and its value is the
right-most amount on that visual row — the "total" column of the form. Every
value is anchored to the label and amount words it was read from.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .bank import row_text, visual_rows
from .extract import XFact
from .pages import Page, anchor_span, anchor_words

AMOUNT = re.compile(r"^[\d,]+\.\d{2}$")
LINES = (
    # field, label pattern on the row (lowercased, whitespace collapsed)
    ("income_from_salary", r'income chargeable under the head "?salaries'),
    ("gross_total_income", r"gross total income"),
    ("total_taxable_income", r"total taxable income"),
    ("tax_on_total_income", r"tax on total income"),
    ("net_tax_payable", r"net tax payable"),
)
AY_RE = re.compile(r"Assessment Year\s*:?\s*(\d{4}-\d{2})")


def extract_part_b(pages: list[Page]) -> tuple[list[XFact], list[str]]:
    facts: list[XFact] = []
    issues: list[str] = []
    for p in pages:
        m = AY_RE.search(p.text)
        if m:
            facts.append(XFact("assessment_year", "text", f"AY {m.group(1)}", "parser", True,
                               anchor=anchor_span(p, m.start(1), m.end(1)), page_no=p.page_no))
            break
    gross_seen = False
    for p in pages:
        for row in visual_rows(p.words()):
            t = " ".join(row_text(row).lower().split())
            amounts = [w for w in row if AMOUNT.match(w.text)]
            if "gross salary" in t and not amounts:
                gross_seen = True
            if gross_seen and re.match(r"^\(?d\)?\s*total\b", t) and amounts and \
                    not any(f.field == "gross_salary" for f in facts):
                facts.append(_fact(p, "gross_salary", row, amounts[-1]))
                gross_seen = False
            for field, pattern in LINES:
                if re.search(pattern, t) and amounts and not any(f.field == field for f in facts):
                    facts.append(_fact(p, field, row, amounts[-1]))
    have = {f.field for f in facts}
    for need in ("assessment_year", "income_from_salary", "gross_total_income", "total_taxable_income"):
        if need not in have:
            issues.append(f"{need.replace('_', ' ').capitalize()} not found.")
    return facts, issues


def _fact(page: Page, field: str, row, amount) -> XFact:
    label = [w for w in row if w.x1 <= amount.x0]
    words = (label[:8] if label else []) + [amount]
    return XFact(field, "amount", Decimal(amount.text.replace(",", "")), "parser", True,
                 anchor=anchor_words(page, words), page_no=page.page_no)
