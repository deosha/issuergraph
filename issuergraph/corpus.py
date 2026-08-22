"""The IIFL Finance corpus: what to fetch, and which extractor reads it.

Explicit and ordered. No discovery, no agent deciding what to open.
"""
from __future__ import annotations

from dataclasses import dataclass

from .extractors import annual_report, brickwork, care, icra
from .models import DocumentMeta


@dataclass(frozen=True)
class Source:
    filename: str
    url: str
    meta: DocumentMeta
    extractor: object


ISSUER = {
    "name": "IIFL Finance Limited",
    "aliases": ["IIFL Finance", "IIFL Finance Ltd", "IIFL", "India Infoline Finance Limited"],
    "cin": "L67100MH1995PLC093797",
}

SOURCES: tuple[Source, ...] = (
    Source(
        filename="ar2025.pdf",
        url="https://storage.googleapis.com/iifl-finance-storage/files/investor/financials/Annual_Report_2024-2025.pdf",
        meta=DocumentMeta(doc_type="annual_report", source_name="Company",
                          title="IIFL Finance Integrated Annual Report 2024-25",
                          url="https://storage.googleapis.com/iifl-finance-storage/files/investor/financials/Annual_Report_2024-2025.pdf"),
        extractor=annual_report,
    ),
    Source(
        filename="icra_126125.pdf",
        url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=126125",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="ICRA",
                          title="ICRA: Long-term ratings placed on Watch with Negative Implications",
                          url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=126125"),
        extractor=icra,
    ),
    Source(
        filename="icra_137962.pdf",
        url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=137962",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="ICRA",
                          title="ICRA: Ratings reaffirmed and outlook revised to Negative",
                          url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=137962"),
        extractor=icra,
    ),
    Source(
        filename="icra_140856.pdf",
        url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=140856",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="ICRA",
                          title="ICRA: Ratings reaffirmed",
                          url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=140856"),
        extractor=icra,
    ),
    Source(
        filename="care_20240315.pdf",
        url="https://www.careratings.com/upload/CompanyFiles/PR/202403150346_IIFL_Finance_Limited.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CARE",
                          title="CARE Ratings press release (Revised)",
                          url="https://www.careratings.com/upload/CompanyFiles/PR/202403150346_IIFL_Finance_Limited.pdf"),
        extractor=care,
    ),
    Source(
        filename="bwr_20250915.pdf",
        url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-15Sep2025.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="Brickwork",
                          title="Brickwork Ratings rationale",
                          url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-15Sep2025.pdf"),
        extractor=brickwork,
    ),
)
