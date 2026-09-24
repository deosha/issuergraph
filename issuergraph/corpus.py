"""The IIFL Finance corpus: what to fetch, and which extractor reads it.

Explicit and ordered. No discovery, no agent deciding what to open.
"""
from __future__ import annotations

from dataclasses import dataclass

from .extractors import annual_report, brickwork, care, crisil, icra
from .models import DocumentMeta


@dataclass(frozen=True)
class Source:
    filename: str
    url: str
    meta: DocumentMeta
    extractor: object
    # 'text/html' sources keep their bytes in the database and are anchored by
    # node path (htmldoc.py); everything else is a PDF.
    media_type: str = "application/pdf"


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
        filename="icra_130090.pdf",
        url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=130090",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="ICRA",
                          title="ICRA: Ratings reaffirmed; long-term ratings removed from Rating Watch",
                          url="https://www.icra.in/Rating/GetRationalReportFilePdf?id=130090"),
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
        filename="care_20240413.pdf",
        url="https://www.careratings.com/upload/CompanyFiles/PR/202404130415_IIFL_Finance_Limited.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CARE",
                          title="CARE Ratings press release: watch direction revised to Negative",
                          url="https://www.careratings.com/upload/CompanyFiles/PR/202404130415_IIFL_Finance_Limited.pdf"),
        extractor=care,
    ),
    Source(
        filename="care_20240917.pdf",
        url="https://www.careratings.com/upload/CompanyFiles/PR/202409170957_IIFL_Finance_Limited.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CARE",
                          title="CARE Ratings press release: long-term ratings revised",
                          url="https://www.careratings.com/upload/CompanyFiles/PR/202409170957_IIFL_Finance_Limited.pdf"),
        extractor=care,
    ),
    Source(
        filename="care_20240920.pdf",
        url="https://www.careratings.com/upload/CompanyFiles/PR/202409150931_IIFL_Finance_Limited.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CARE",
                          title="CARE Ratings press release: bank facilities and subordinated debt withdrawn",
                          url="https://www.careratings.com/upload/CompanyFiles/PR/202409150931_IIFL_Finance_Limited.pdf"),
        extractor=care,
    ),
    Source(
        filename="crisil_20240930.html",
        url="https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_September%2030_%202024_RR_353257.html",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CRISIL",
                          title="Crisil Ratings rationale: removed from watch, reaffirmed",
                          url="https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_September%2030_%202024_RR_353257.html"),
        extractor=crisil,
        media_type="text/html",
    ),
    Source(
        filename="crisil_20260127.html",
        url="https://www.crisil.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_January%2027_%202026_RR_387837.html",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CRISIL",
                          title="Crisil Ratings rationale: ratings reaffirmed",
                          url="https://www.crisil.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_January%2027_%202026_RR_387837.html"),
        extractor=crisil,
        media_type="text/html",
    ),
    Source(
        filename="bwr_20250915.pdf",
        url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-15Sep2025.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="Brickwork",
                          title="Brickwork Ratings rationale",
                          url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-15Sep2025.pdf"),
        extractor=brickwork,
    ),
    Source(
        filename="bwr_20251121.pdf",
        url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-21Nov2025.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="Brickwork",
                          title="Brickwork Ratings rationale, November 2025",
                          url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-21Nov2025.pdf"),
        extractor=brickwork,
    ),
    Source(
        filename="bwr_20251224.pdf",
        url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-24Dec2025.pdf",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="Brickwork",
                          title="Brickwork Ratings rationale, December 2025",
                          url="https://www.brickworkratings.com/Admin/PressRelease/IIFL-Finance-24Dec2025.pdf"),
        extractor=brickwork,
    ),
    Source(
        filename="crisil_20260324.html",
        url="https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_March%2024_%202026_RR_392225.html",
        meta=DocumentMeta(doc_type="rating_rationale", source_name="CRISIL",
                          title="Crisil Ratings rationale, March 2026",
                          url="https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/IIFLFinanceLimited_March%2024_%202026_RR_392225.html"),
        extractor=crisil,
        media_type="text/html",
    ),
)
