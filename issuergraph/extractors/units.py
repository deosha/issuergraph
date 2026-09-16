"""Monetary units, read from the document rather than assumed.

An anchor proves a number was printed where we say it was. It proves nothing
about what the number *means*: change a statement's header from crores to lakhs
and every digit on the page stays exactly where it was, so every anchor still
verifies while every value is now wrong by a factor of 100.

So the unit is extracted, not hardcoded. `find_unit` locates the statement's own
declaration — "(` in Crores)", "(₹ in lakhs)" — and returns its character span,
which the extractor attaches to each claim as an additional anchor. The unit
then has the same standard of proof as the amount: a reader clicking the fact
sees the header that establishes its scale.

A page with no recognisable declaration yields no unit, and the caller must
treat that as an extraction failure. Guessing "probably crores because Indian
NBFC balance sheets usually are" is the assumption this module exists to delete.
"""
from __future__ import annotations

import re
from decimal import Decimal

CANONICAL = "INR_CRORE"

# PyMuPDF renders the rupee glyph of several Indian annual-report fonts as a
# backtick, so the character before "in" is matched permissively rather than
# assumed to be "₹".
# The scale is captured as any word, not as an alternation of the scales we
# happen to support. Matching only known scales would make an unfamiliar one
# ("in trillions") look identical to no declaration at all, and those two need
# different reports: one is a document we cannot read, the other a document
# that never said.
UNIT_RE = re.compile(
    r"\(\s*[`₹]?\s*(?:Rs\.?|INR|₹|`)?\s*in\s+(?P<scale>[A-Za-z]{3,12})\s*\)",
    re.IGNORECASE,
)

# Multiplier from the declared scale to one crore (10^7).
TO_CRORE: dict[str, Decimal] = {
    "crore": Decimal(1),
    "lakh": Decimal("0.01"),
    "lac": Decimal("0.01"),
    "million": Decimal("0.1"),
    "billion": Decimal(100),
    "thousand": Decimal("0.0001"),
}


class UnknownUnit(ValueError):
    """The page declares a scale this module does not know how to convert."""


class Unit:
    """A declared monetary scale and the text that declares it.

    `span` is what makes this evidence rather than configuration: it is a
    character range on the same page as the amount, so it becomes an anchor.
    """

    __slots__ = ("scale", "factor", "span", "text")

    def __init__(self, scale: str, factor: Decimal, span: tuple[int, int], text: str):
        self.scale = scale
        self.factor = factor
        self.span = span
        self.text = text

    @property
    def is_canonical(self) -> bool:
        return self.factor == 1

    def to_crore(self, amount: Decimal) -> Decimal:
        """Convert, preserving the two-decimal presentation of Indian statements."""
        converted = amount * self.factor
        return converted.quantize(Decimal("0.01")) if self.factor != 1 else amount

    def __repr__(self) -> str:
        return f"Unit({self.scale!r}, x{self.factor}, span={self.span})"


def _singular(scale: str) -> str:
    lowered = scale.lower()
    return lowered[:-1] if lowered.endswith("s") else lowered


def find_unit(text: str, before: int | None = None) -> Unit | None:
    """The unit declaration governing amounts on this page.

    `before` restricts the search to text preceding an offset — statement
    headers sit above their table, and a later page section can declare a
    different scale. The last declaration before the table wins, being the
    nearest one above it.
    """
    window = text if before is None else text[:before]
    matches = list(UNIT_RE.finditer(window))
    if not matches:
        return None
    match = matches[-1]
    scale = _singular(match.group("scale"))
    if scale not in TO_CRORE:
        raise UnknownUnit(f"unrecognised scale {match.group('scale')!r}")
    return Unit(scale, TO_CRORE[scale], (match.start(), match.end()), match.group(0))
