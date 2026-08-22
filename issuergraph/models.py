"""Pydantic models. These are the contract between extractors and the loader.

An ExtractedClaim without anchors cannot be constructed — that is the
"no source = no fact" rule expressed as a type.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Basis = Literal["standalone", "consolidated", "unknown"]
ClaimType = Literal["total_borrowings", "debt_instrument", "rating", "rationale_point"]


class Anchor(BaseModel):
    """A character range on one page of one document."""

    page_no: int = Field(ge=1)
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


class RatingDetail(BaseModel):
    agency: str
    instrument: str
    rated_amount_cr: Decimal | None = None
    rating: str
    outlook: str | None = None
    watch: str | None = None
    action: str | None = None
    previous_rating: str | None = None
    action_date: date | None = None


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

    @model_validator(mode="after")
    def _has_value(self):
        if self.value_numeric is None and self.value_text is None:
            raise ValueError("claim must carry a numeric or textual value")
        return self


class DocumentMeta(BaseModel):
    doc_type: Literal["annual_report", "rating_rationale"]
    source_name: str
    title: str
    url: str
    published_date: date | None = None
