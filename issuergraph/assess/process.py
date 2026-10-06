"""Receive, store and process one case's documents.

A document is stored once per case (SHA-256), under data/assess/ (gitignored),
and processed once per extractor version: reopening a case reads the stored
results, it does not repeat model or OCR calls. `force=True` reprocesses.

Every write re-verifies its anchor against the stored page text (or, for ITR
JSON, re-resolves the JSON path against the stored bytes) — the same rule as
the issuer loader. A failure leaves the document `failed` with its reason;
it never appears complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from datetime import date, datetime, timezone
from decimal import Decimal

from psycopg.types.json import Jsonb

from ..db import connect
from . import bank, extract, form16, registration
from .classify import classify
from .pages import AnchorError, Page, PasswordProtected, needs_ocr, pdf_pages, verify
from .provider import ProviderUnavailable, Usage, load_fixture_provider, model_provider, ocr_function

ROOT = pathlib.Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "assess"
FIXTURE_DIR_NAME = "synthetic"
KINDS = ("salary_slip", "bank_statement", "credit_report", "itr_json", "od_sanction", "other",
         "form16_part_a", "form16_part_b", "tax_statement", "itr_ack", "kyc", "loan_sanction",
         "gst_certificate", "udyam_certificate", "financials", "itr_computation", "business_proof",
         "loan_soa")
# Recognised, deliberately not extracted. KYC documents keep no page text at all.
IDENTIFY_ONLY = {
    "kyc": "Identity document: recognised, not extracted, no text kept.",
    "tax_statement": "Annual tax statement (AIS/26AS): recognised; not extracted in this version.",
    "form16_part_a": "Form 16 Part A (TDS certificate): recognised; salary figures are read from Part B.",
    "itr_ack": "ITR acknowledgement: recognised; not extracted in this version.",
    "financials": "Financial statements (P&L, balance sheet): recognised; not extracted in this version.",
    "itr_computation": "Income-tax computation: recognised; not extracted in this version.",
    "business_proof": "Business address / licence proof: recognised, not extracted.",
    "loan_soa": "Loan statement of account: recognised; not extracted in this version.",
    "loan_sanction": "Sanction letter for a new loan (not an overdraft): an offer, not an existing "
                     "obligation, so it is not counted as a liability. Terms not extracted.",
}
EXTRACTOR = {"bank_statement": ("bank", bank.VERSION), "salary_slip": ("slip", extract.VERSION),
             "credit_report": ("credit_report", extract.VERSION),
             "itr_json": ("itr_json", extract.VERSION), "od_sanction": ("od_sanction", extract.VERSION),
             "form16_part_b": ("form16", "form16-1"),
             "gst_certificate": ("registration", "registration-1"),
             "udyam_certificate": ("registration", "registration-1"), "other": ("classify", "classify-1"),
             **{k: ("identify", "identify-1") for k in IDENTIFY_ONLY}}


# --- cases & documents ---------------------------------------------------------

def create_case(label: str, applicant_name: str | None, borrower_type: str = "salaried",
                requested_product: str | None = None, requested_amount=None,
                synthetic: bool = False) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO assess.case_file (label, applicant_name, borrower_type, requested_product, "
            "requested_amount, is_synthetic) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (label, applicant_name, borrower_type, requested_product, requested_amount, synthetic),
        ).fetchone()["id"]


def guess_kind(filename: str, head_text: str = "") -> str:
    n = filename.lower()
    t = head_text.lower()
    if n.endswith(".json"):
        return "itr_json"
    if any(k in n for k in ("slip", "payslip", "salary")) or "pay slip" in t or "payslip" in t:
        return "salary_slip"
    if any(k in n for k in ("cibil", "credit", "bureau", "experian", "crif", "equifax")) or "credit score" in t:
        return "credit_report"
    if "sanction" in n or "sanction" in t[:2000]:
        return "od_sanction"
    if any(k in n for k in ("statement", "stmt", "acct", "account")) or "statement" in t[:2000]:
        return "bank_statement"
    return "other"


def add_document(case_id: int, src: pathlib.Path, kind: str | None = None,
                 filename: str | None = None) -> tuple[int, bool]:
    """(document id, newly added?). Copies the file into the private store."""
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    filename = filename or src.name
    media = "application/json" if filename.lower().endswith(".json") else "application/pdf"
    if media == "application/pdf" and not data.startswith(b"%PDF"):
        media = "application/octet-stream"
    basis = "Set when added." if kind else None
    if kind is None:
        kind = "itr_json" if media == "application/json" else "other"   # classified from content when processed
        basis = "JSON file." if media == "application/json" else None
    if kind not in KINDS:
        raise ValueError(f"unknown document kind {kind!r}")
    with connect() as conn:
        existing = conn.execute("SELECT id FROM assess.document WHERE case_id=%s AND sha256=%s",
                                (case_id, digest)).fetchone()
        if existing:
            return existing["id"], False
        folder = STORE / f"case_{case_id}"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        dest = folder / f"{digest[:12]}_{pathlib.Path(filename).name}"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        doc_id = conn.execute(
            "INSERT INTO assess.document (case_id, kind, filename, sha256, byte_size, stored_path, "
            "media_type, kind_basis) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (case_id, kind, pathlib.Path(filename).name, digest, len(data), str(dest), media, basis),
        ).fetchone()["id"]
    return doc_id, True


def unlock(doc_id: int, password: str) -> None:
    """Replace a password-protected PDF with a decrypted copy of the same pages.

    The decrypted copy becomes the evidence of record (its hash is the
    document's hash from now on); the original file's hash is kept in the
    summary. The password is used once and not stored or logged.
    """
    import pymupdf

    with connect() as conn:
        doc = conn.execute("SELECT * FROM assess.document WHERE id=%s", (doc_id,)).fetchone()
        pdf = pymupdf.open(doc["stored_path"])
        if not pdf.needs_pass:
            raise ValueError("This PDF is not password-protected.")
        if not pdf.authenticate(password):
            raise ValueError("Wrong password.")
        path = pathlib.Path(doc["stored_path"])
        out = path.with_name(path.stem + "_unlocked.pdf")
        pdf.save(out, encryption=pymupdf.PDF_ENCRYPT_NONE)
        pdf.close()
        os.chmod(out, 0o600)
        data = out.read_bytes()
        conn.execute("UPDATE assess.document SET stored_path=%s, sha256=%s, byte_size=%s, status='received', "
                     "summary = summary || %s WHERE id=%s",
                     (str(out), hashlib.sha256(data).hexdigest(), len(data),
                      Jsonb({"decrypted_from_sha256": doc["sha256"]}), doc_id))
        path.unlink()


def import_folder(case_id: int, folder: pathlib.Path) -> list[int]:
    """Local import: every PDF and JSON directly in `folder` (no recursion)."""
    manifest = {}
    mf = folder / "manifest.json"
    if mf.exists():
        manifest = {m["file"]: m["kind"] for m in json.loads(mf.read_text())}
    ids = []
    for p in sorted(folder.iterdir()):
        if p.name in ("manifest.json", "model_responses.json") or p.suffix.lower() not in (".pdf", ".json"):
            continue
        ids.append(add_document(case_id, p, manifest.get(p.name))[0])
    return ids


# --- processing --------------------------------------------------------------------

def process_case(case_id: int, force: bool = False) -> list[dict]:
    with connect() as conn:
        docs = conn.execute("SELECT id FROM assess.document WHERE case_id=%s ORDER BY id",
                            (case_id,)).fetchall()
    return [process_document(d["id"], force=force) for d in docs]


def _provider_for(case: dict, doc: dict):
    """The model provider for this document, or (None, reason)."""
    if case["is_synthetic"]:
        fx = load_fixture_provider(pathlib.Path(doc["stored_path"]).parent / FIXTURE_DIR_NAME)
        return (fx, None) if fx else (None, "No synthetic responses recorded.")
    try:
        return model_provider(), None
    except ProviderUnavailable as exc:
        return None, str(exc)


def process_document(doc_id: int, force: bool = False) -> dict:
    _classify_if_needed(doc_id)
    with connect() as conn:
        doc = conn.execute("SELECT * FROM assess.document WHERE id=%s", (doc_id,)).fetchone()
        case = conn.execute("SELECT * FROM assess.case_file WHERE id=%s", (doc["case_id"],)).fetchone()
        extractor, version = EXTRACTOR[doc["kind"]]
        if not force and doc["status"] in ("complete", "incomplete", "unsupported", "identified"):
            last = conn.execute("SELECT version FROM assess.extraction_run WHERE document_id=%s "
                                "AND status <> 'running' ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()
            if last and last["version"] == version:
                return {"id": doc_id, "status": doc["status"], "cached": True}
        conn.execute("UPDATE assess.document SET status='processing' WHERE id=%s", (doc_id,))
        run_id = conn.execute("INSERT INTO assess.extraction_run (document_id, extractor, version) "
                              "VALUES (%s,%s,%s) RETURNING id", (doc_id, extractor, version)).fetchone()["id"]

    usage = Usage()
    pages: list[Page] = []
    try:
        with connect() as conn:
            for table in ("txn", "item", "page"):
                conn.execute(f"DELETE FROM assess.{table} WHERE document_id=%s", (doc_id,))
            result = _extract(conn, case, doc, usage, pages)
            status, reasons = result
            conn.execute(
                "UPDATE assess.document SET status=%s, status_reasons=%s, processed_at=now(), "
                "page_count=%s WHERE id=%s",
                (status, Jsonb(reasons), len(pages), doc_id))
            _finish_run(conn, run_id, "ok", usage, len(pages))
        return {"id": doc_id, "status": status, "reasons": reasons, "cached": False}
    except Exception as exc:  # noqa: BLE001 — every failure must be visible, none fatal to the case
        reason = str(exc) if isinstance(exc, (ProviderUnavailable, AnchorError, PasswordProtected)) else \
            f"Extraction error ({type(exc).__name__})."
        with connect() as conn:
            for table in ("txn", "item"):
                conn.execute(f"DELETE FROM assess.{table} WHERE document_id=%s", (doc_id,))
            conn.execute("UPDATE assess.document SET status='failed', status_reasons=%s, "
                         "processed_at=now() WHERE id=%s", (Jsonb([reason]), doc_id))
            _finish_run(conn, run_id, "failed", usage, len(pages), error=f"{type(exc).__name__}: {exc}"[:500])
        return {"id": doc_id, "status": "failed", "reasons": [reason], "cached": False}


def _classify_if_needed(doc_id: int) -> None:
    """Identify an untyped PDF from its content, committed on its own so a
    later extraction failure does not lose what the document is."""
    with connect() as conn:
        doc = conn.execute("SELECT * FROM assess.document WHERE id=%s", (doc_id,)).fetchone()
        if doc["kind"] != "other" or doc["kind_basis"] is not None or doc["media_type"] != "application/pdf":
            return
        try:
            found, _ = pdf_pages(doc["stored_path"])
        except PasswordProtected:
            return
        kind, basis = classify(found, doc["filename"])
        conn.execute("UPDATE assess.document SET kind=%s, kind_basis=%s WHERE id=%s", (kind, basis, doc_id))


def _finish_run(conn, run_id, status, usage: Usage, pages: int, error: str | None = None):
    conn.execute(
        "UPDATE assess.extraction_run SET finished_at=now(), status=%s, pages=%s, ocr_pages=%s, "
        "model_id=%s, model_calls=%s, input_tokens=%s, output_tokens=%s, retries=%s, error=%s "
        "WHERE id=%s",
        (status, pages, usage.ocr_pages, usage.model_id, usage.calls, usage.input_tokens,
         usage.output_tokens, usage.retries, error, run_id))


def _load_pages(conn, doc, usage: Usage, pages: list[Page]) -> list[str]:
    reasons = []
    found, _ = pdf_pages(doc["stored_path"])
    scanned = needs_ocr(found)
    if scanned:
        ocr = ocr_function()
        if ocr is not None:
            found, usage.ocr_pages = pdf_pages(doc["stored_path"], ocr=ocr)
            scanned = needs_ocr(found)
        else:
            reasons.append("No text layer on page(s) " + ", ".join(map(str, scanned)) +
                           " and OCR (Textract) is not configured; those pages were not read.")
    pages.extend(found)
    with conn.cursor() as cur:
        for p in found:
            cur.execute("INSERT INTO assess.page (document_id, page_no, text, width, height, word_map, "
                        "text_source) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (doc["id"], p.page_no, p.text, p.width, p.height, Jsonb(p.word_map), p.text_source))
    return reasons


def _extract(conn, case, doc, usage: Usage, pages: list[Page]) -> tuple[str, list[str]]:
    kind = doc["kind"]
    if doc["media_type"] not in ("application/pdf", "application/json"):
        return "unsupported", ["Only PDF documents and ITR JSON are supported in this demo."]
    if kind in IDENTIFY_ONLY:
        return "identified", [IDENTIFY_ONLY[kind]]
    if kind == "other":
        return "unsupported", [(doc["kind_basis"] or "Type not recognised.") +
                               " Choose a type to process it."]
    if kind == "itr_json":
        raw = pathlib.Path(doc["stored_path"]).read_bytes()
        facts, summary, issues = extract.extract_itr(raw)
        if summary.get("json_type") == "unknown":
            return "unsupported", issues
        item = _item(conn, doc, "itr", 0, summary.get("assessment_year") or "ITR")
        for f in facts:
            _store_fact(conn, doc, item, f, pages, raw_json=raw)
        conn.execute("UPDATE assess.document SET summary=%s, source_label=%s WHERE id=%s",
                     (Jsonb(summary), "Income-tax return (JSON)", doc["id"]))
        if summary.get("json_type") == "prefill":
            return "incomplete", issues
        need = {"assessment_year"} - {f.field for f in facts}
        if need or not ({"gross_salary", "total_income"} & {f.field for f in facts}):
            return "incomplete", issues + ["Assessment year or income figures not found."]
        return ("complete" if not issues else "incomplete"), issues

    if doc["media_type"] != "application/pdf":
        return "unsupported", [f"A {kind.replace('_', ' ')} must be a PDF."]
    reasons = _load_pages(conn, doc, usage, pages)
    if not any(p.text.strip() for p in pages):
        return "failed", reasons or ["No readable text in the document."]

    if kind in ("gst_certificate", "udyam_certificate"):
        facts, issues = registration.extract(pages, kind)
        item = _item(conn, doc, "registration", 0, "GST" if kind == "gst_certificate" else "Udyam")
        for f in facts:
            _store_fact(conn, doc, item, f, pages)
        dates = [f.value for f in facts if f.value_kind == "date"]
        conn.execute("UPDATE assess.document SET doc_date=%s, source_label=%s WHERE id=%s",
                     (min(dates) if dates else None,
                      "GST registration" if kind == "gst_certificate" else "Udyam registration", doc["id"]))
        problems = reasons + issues
        return ("complete" if not problems else "incomplete"), problems

    if kind == "form16_part_b":
        facts, issues = form16.extract_part_b(pages)
        ay = next((f.value for f in facts if f.field == "assessment_year"), None)
        item = _item(conn, doc, "form16", 0, ay or "Form 16")
        for f in facts:
            _store_fact(conn, doc, item, f, pages)
        conn.execute("UPDATE assess.document SET summary=%s, source_label=%s WHERE id=%s",
                     (Jsonb({"assessment_year": ay}), "Form 16 Part B (employer)", doc["id"]))
        problems = reasons + issues
        return ("complete" if not problems else "incomplete"), problems

    if kind == "bank_statement":
        st = bank.parse_statement(pages, doc["stored_path"])
        item = _item(conn, doc, "statement", 0, f"{st.bank or 'Bank'} {st.account_masked or ''}".strip())
        for f in st.facts:
            _store_fact(conn, doc, item, extract.XFact(f.field, f.value_kind, f.value, "parser", True,
                                                       anchor=f.anchor, note=f.note), pages)
        key = f"{st.bank or 'unknown'}|{st.account_masked or 'doc' + str(doc['id'])}"
        by_no = {p.page_no: p for p in pages}
        with conn.cursor() as cur:
            for r in st.rows:
                a = r.anchor
                verify(by_no[a["page_no"]].text, a["spans"], a["evidence_text"])
                cur.execute(
                    "INSERT INTO assess.txn (case_id, document_id, account_key, seq, txn_date, value_date, "
                    "narration, ref, debit, credit, balance, page_no, spans, rects, evidence_text) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc["case_id"], doc["id"], key, r.seq, r.txn_date, r.value_date, r.narration, r.ref,
                     r.debit, r.credit, r.balance, r.page_no, Jsonb(a["spans"]), Jsonb(a["rects"]),
                     a["evidence_text"]))
        summary = dict(st.summary, bank=st.bank, account=st.account_masked, account_key=key,
                       account_type=st.account_type,
                       issues=st.issues)
        conn.execute("UPDATE assess.document SET summary=%s, source_label=%s, period_start=%s, "
                     "period_end=%s, doc_date=%s WHERE id=%s",
                     (Jsonb(summary), st.bank, st.period[0] if st.period else None,
                      st.period[1] if st.period else None, st.period[1] if st.period else None, doc["id"]))
        if not st.rows:
            return "unsupported", reasons + st.issues
        problems = reasons + st.issues
        if st.period is None:
            problems.append("Statement period not found.")
        if st.account_masked is None:
            problems.append("Account number not found: overlapping statements of this account "
                            "cannot be de-duplicated.")
        return ("complete" if not problems else "incomplete"), problems

    provider, why = _provider_for(case, doc)
    if kind == "salary_slip":
        facts, notes = extract.extract_slip(pages, provider, usage, doc["sha256"])
        if why and notes:
            notes = [n + f" ({why})" for n in notes]
        values = {f.field: f for f in facts}
        month = values.get("pay_month")
        label = f"{month.value:%b %Y}" if month and month.value else doc["filename"]
        item = _item(conn, doc, "salary_slip", 0, label)
        for f in facts:
            _store_fact(conn, doc, item, f, pages)
        employer = values.get("employer")
        if month and month.value:
            end = (date(month.value.year + month.value.month // 12, month.value.month % 12 + 1, 1))
            conn.execute("UPDATE assess.document SET doc_date=%s, period_start=%s, period_end=%s, "
                         "source_label=%s WHERE id=%s",
                         (month.value, month.value, end.fromordinal(end.toordinal() - 1),
                          employer.value if employer else None, doc["id"]))
        problems = reasons + notes
        for need in ("pay_month", "net_pay", "gross_pay", "total_deductions"):
            if need not in values:
                problems.append(f"{need.replace('_', ' ').capitalize()} not found.")
            elif not values[need].verified:
                problems.append(f"{need.replace('_', ' ').capitalize()} is unverified.")
        if "pay_month" not in values or "net_pay" not in values:
            return "failed", problems
        return ("complete" if not problems else "incomplete"), problems

    if kind == "credit_report":
        try:
            head, accounts = extract.extract_credit_report(pages, provider, usage, doc["sha256"])
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(f"{exc} {why or ''}".strip()) from None
        item = _item(conn, doc, "credit_report", 0, "Report")
        for f in head:
            _store_fact(conn, doc, item, f, pages)
        for n, facts in enumerate(accounts, 1):
            v = {f.field: f.value for f in facts}
            t = _item(conn, doc, "tradeline", n, f"{v.get('lender') or 'Unknown lender'} · "
                                                  f"{v.get('account_type') or 'account'}")
            for f in facts:
                _store_fact(conn, doc, t, f, pages)
        hv = {f.field: f for f in head}
        rd = hv.get("report_date")
        conn.execute("UPDATE assess.document SET doc_date=%s, source_label=%s, summary=%s WHERE id=%s",
                     (rd.value if rd and rd.verified else None,
                      hv["bureau"].value if "bureau" in hv else None,
                      Jsonb({"accounts": len(accounts), "supplied_by": "applicant"}), doc["id"]))
        problems = list(reasons)
        if not accounts and why:
            problems.append(why)
        if not rd:
            problems.append("Report date not found.")
        if "score" not in hv and "score_code" not in hv:
            problems.append("No score or no-score state found.")
        unverified = sum(1 for f in head if not f.verified) + \
            sum(1 for facts in accounts for f in facts if not f.verified)
        if unverified:
            problems.append(f"{unverified} field(s) proposed by the model could not be verified "
                            "against the report; they are excluded until reviewed.")
        if not accounts:
            problems.append("No accounts were read. A report with no accounts must be confirmed "
                            "by the analyst, not assumed.")
        declared = extract.declared_account_count(pages)
        if declared is not None and declared != len(accounts):
            problems.append(f"The report lays out {declared} account(s) (Account Number labels); "
                            f"{len(accounts)} were read. Check for missing or duplicated accounts.")
        return ("complete" if not problems else "incomplete"), problems

    if kind == "od_sanction":
        if provider is None:
            raise ProviderUnavailable(why or "No model provider.")
        facts = extract.extract_od_sanction(pages, provider, usage, doc["sha256"])
        item = _item(conn, doc, "od_sanction", 0, "OD sanction")
        for f in facts:
            _store_fact(conn, doc, item, f, pages)
        v = {f.field: f for f in facts}
        problems = list(reasons)
        if "sanctioned_limit" not in v or not v["sanctioned_limit"].verified:
            problems.append("Sanctioned limit not verified.")
        if "sanction_date" in v and v["sanction_date"].verified:
            conn.execute("UPDATE assess.document SET doc_date=%s WHERE id=%s",
                         (v["sanction_date"].value, doc["id"]))
        return ("complete" if not problems else "incomplete"), problems
    return "unsupported", ["No extractor for this document type."]


def _item(conn, doc, item_type, seq, label) -> int:
    return conn.execute("INSERT INTO assess.item (case_id, document_id, item_type, seq, label) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (doc["case_id"], doc["id"], item_type, seq, label)).fetchone()["id"]


def _store_fact(conn, doc, item_id, f: extract.XFact, pages: list[Page], raw_json: bytes | None = None):
    num = text = dt = None
    if isinstance(f.value, (Decimal, int)) and not isinstance(f.value, bool):
        num = f.value
    elif isinstance(f.value, date):
        dt = f.value
    elif f.value is not None:
        text = str(f.value)
    spans = rects = evidence = None
    page_no = f.page_no
    if f.verified and f.anchor:
        a = f.anchor
        page = next(p for p in pages if p.page_no == a["page_no"])
        verify(page.text, a["spans"], a["evidence_text"])
        spans, rects, evidence, page_no = a["spans"], a["rects"], a["evidence_text"], a["page_no"]
    elif f.verified and f.json_path:
        _verify_json(raw_json, f.json_path, f.value)
        evidence = f"{f.json_path} = {f.value}"
    elif f.verified:
        raise AnchorError(f"verified fact {f.field} has no anchor")
    else:
        evidence = f.quote
    if f.value is None and text is None and f.verified:
        text = "reported blank"
    conn.execute(
        "INSERT INTO assess.fact (item_id, case_id, document_id, field, value_kind, value_num, "
        "value_text, value_date, origin, verified, page_no, spans, rects, evidence_text, json_path, note) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (item_id, doc["case_id"], doc["id"], f.field, f.value_kind, num, text, dt, f.origin, f.verified,
         page_no, Jsonb(spans) if spans else None, Jsonb(rects) if rects else None, evidence,
         f.json_path, f.note))


def _verify_json(raw: bytes | None, path: str, value) -> None:
    obj = json.loads(raw)
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    if isinstance(value, Decimal):
        ok = Decimal(str(obj)) == value
    else:
        ok = str(obj) in str(value)
    if not ok:
        raise AnchorError(f"JSON path {path} does not hold the stored value")


def now() -> datetime:
    return datetime.now(timezone.utc)
