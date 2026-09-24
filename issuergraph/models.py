"""Pydantic models. These are the contract between extractors and the loader.

An ExtractedClaim without anchors cannot be constructed — that is the
"no source = no fact" rule expressed as a type.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Basis = Literal["standalone", "consolidated", "unknown", "agency_adjusted"]


def normalize_value(value_text: str | None) -> str | None:
    """The comparison form of a textual value.

    "Negative", "NEGATIVE" and "Negative " are the same rating outlook; only
    the raw text is kept for evidence display. Reconciliation compares this.
    Mirrored in sql/002_conflict_history.sql for the backfill.
    """
    if value_text is None:
        return None
    return " ".join(value_text.split()).casefold() or None
ClaimType = Literal["total_borrowings", "debt_instrument", "rating", "rationale_point",
                    "rating_history", "financial_indicator"]
# What the document says it is doing now, or what its rating-history annexure
# says the agency did before (sql/009). None lets the loader derive the primary
# kind from the document type; a history claim must say so itself.
Provenance = Literal["primary_rationale", "primary_report", "history_annexure"]


class Anchor(BaseModel):
    """A character range in one document.

    Two kinds, one contract — the quoted text equals the characters at the
    stored offsets, re-checked by the loader:

      * 'pdf': offsets into the canonical text of page `page_no`, which maps to
        word rectangles for highlighting;
      * 'html': offsets into `text_content()` of the node at `node_path` (lxml
        getpath() on the parsed raw HTML), exactly as lxml returns it — no
        whitespace normalisation before indexing.
    """

    kind: Literal["pdf", "html"] = "pdf"
    page_no: int | None = Field(default=None, ge=1)
    node_path: str | None = None
    char_start: int = Field(ge=0)
    char_end: int
    evidence_text: str
    bbox: list[float] | None = None
    bbox_rects: list[list[float]] | None = None

    @model_validator(mode="after")
    def _range(self):
        if self.char_end <= self.char_start:
            raise ValueError("char_end must exceed char_start")
        if not self.evidence_text.strip():
            raise ValueError("evidence_text must not be empty")
        return self

    @model_validator(mode="after")
    def _locator(self):
        if self.kind == "pdf" and (self.page_no is None or self.node_path is not None):
            raise ValueError("a pdf anchor is located by page_no alone")
        if self.kind == "html" and (self.node_path is None or self.page_no is not None
                                    or self.bbox or self.bbox_rects):
            raise ValueError("an html anchor is located by node_path alone, with no geometry")
        return self


class RatingDetail(BaseModel):
    """One agency's rating of one instrument on one date.

    `instrument_class` and `term` exist so two agencies' ratings can be compared
    only where they are ratings *of the same thing*. Comparing on the publisher's
    own wording cannot work — "Non-convertible debenture programme" (ICRA),
    "Non Convertible Debentures" (CARE) and "NCDs Public Issue" (Brickwork) are
    the same instrument class under three names, while "Long Term Bank
    Facilities" is a different class that happens to sit in the same table.
    """

    agency: str
    instrument: str
    instrument_class: str = "other"
    term: Literal["long_term", "short_term"] = "long_term"
    rated_amount_cr: Decimal | None = None
    # None only for a withdrawal that states no grade (sql/008); anything else
    # that failed to yield a grade is a parse failure, not a rating.
    rating: str | None
    outlook: str | None = None
    watch: str | None = None
    action: str | None = None
    previous_rating: str | None = None
    action_date: date | None = None
    # Publisher-marked qualifiers (sql/011): 'ppmld', 'legacy_r',
    # 'interchangeable_subordinated', 'retail', 'not_yet_issued'.
    qualifiers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _grade_or_withdrawn(self):
        if self.rating is None and "withdrawn" not in (self.action or "").casefold():
            raise ValueError("a rating without a grade must be a withdrawal")
        return self


class HistoryDetail(BaseModel):
    """One row of an agency's rating-history annexure.

    The agency's own record of an action it took, published inside a later
    rationale. Evidence that the action happened and what it said; not the
    rationale for it, which lives in a document we may not hold.
    """

    agency: str
    instrument: str
    instrument_class: str = "other"
    term: Literal["long_term", "short_term"] = "long_term"
    action_date: date
    grade: str | None
    outlook: str | None = None
    watch: str | None = None
    withdrawn: bool = False

    @model_validator(mode="after")
    def _grade_or_withdrawn(self):
        if self.grade is None and not self.withdrawn:
            raise ValueError("a history entry without a grade must be a withdrawal")
        return self


class DebtDetail(BaseModel):
    instrument_name: str
    instrument_type: str
    amount_cr: Decimal
    maturity_date: date | None = None
    secured: bool | None = None
    as_of_date: date | None = None


class ExtractedClaim(BaseModel):
    claim_type: ClaimType
    fact_key: str
    subject: str
    value_numeric: Decimal | None = None
    value_unit: str | None = None
    value_text: str | None = None
    basis: Basis = "unknown"
    as_of_date: date | None = None
    extractor: str
    extractor_version: str
    anchors: list[Anchor] = Field(min_length=1)
    rating: RatingDetail | None = None
    debt: DebtDetail | None = None
    history: HistoryDetail | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _has_value(self):
        if self.value_numeric is None and self.value_text is None:
            raise ValueError("claim must carry a numeric or textual value")
        return self

    @model_validator(mode="after")
    def _history_is_history(self):
        """A history entry is never mistaken for a primary action, either way."""
        is_history = self.claim_type == "rating_history"
        if is_history != (self.history is not None):
            raise ValueError("a rating_history claim, and only one, carries history detail")
        if is_history:
            if self.provenance not in (None, "history_annexure") or self.rating is not None:
                raise ValueError("a history entry is not a primary rating action")
            self.provenance = "history_annexure"
        elif self.provenance == "history_annexure":
            raise ValueError("only a rating_history claim comes from a history annexure")
        return self


class DocumentMeta(BaseModel):
    doc_type: Literal["annual_report", "rating_rationale"]
    source_name: str
    title: str
    url: str
    published_date: date | None = None
