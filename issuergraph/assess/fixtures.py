"""Synthetic documents for tests and the labelled synthetic demo case.

Every document here is invented and says so on its face ("SYNTHETIC — NOT A
REAL DOCUMENT"). Names, account numbers and amounts are fictional. The bank
statements imitate a common netbanking column layout so the parser can be
exercised; they are not any bank's document.

`model_responses.json` holds what a model is *expected* to return for the
credit report — including one wrong page reference and one quote that is not
in the document, so the verification path is exercised end to end. It is
replayed only for these files (keyed by SHA-256).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import date, timedelta
from decimal import Decimal

import pymupdf

FONT = "helv"
STAMP = "SYNTHETIC - NOT A REAL DOCUMENT"
APPLICANT = "Asha Verma (synthetic)"
EMPLOYER = "Northwind Analytics Pvt Ltd"


def _money(v: Decimal) -> str:
    """Indian digit grouping, two decimals."""
    neg = v < 0
    s = f"{abs(v):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def _right(page, x1, y, text, size=7.5):
    w = pymupdf.get_text_length(text, fontname=FONT, fontsize=size)
    page.insert_text((x1 - w, y), text, fontname=FONT, fontsize=size)


def _text(page, x, y, text, size=7.5):
    page.insert_text((x, y), text, fontname=FONT, fontsize=size)


# --- bank statements ------------------------------------------------------------

def _month_rows(year: int, month: int, salary: Decimal, od_interest: Decimal) -> list[dict]:
    last = (date(year, month % 12 + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    mm = f"{month:02d}"
    rows = [
        (date(year, month, 2), f"UPI-FRESHMART STORES-freshmart@okbank-UPI{year}{mm}0201", "0000412300000101", Decimal("2345.00"), None),
        (date(year, month, 5), "ACH D- BAJAJ FINANCE LTD-P400ZZ5678", "0000000000005678", Decimal("18450.00"), None),
        (date(year, month, 7), "NACH DR ICICI BANK LTD AUTO LOAN UMRN ICIC70009876", "0000000000009876", Decimal("12300.00"), None),
        (date(year, month, 10), "ACH D- TATA CAPITAL LTD-OD INTEREST TCFOD0004321", "0000000000004321", od_interest, None),
        (date(year, month, 12), "ACH D- KREDITBEE-KB778899", "0000000000778899", Decimal("4999.00"), None),
        (date(year, month, 15), "CC 000412XXXXXX9012 AUTOPAY SI-TAD", "0000000000009012", Decimal("15000.00"), None),
        (date(year, month, 20), f"IMPS-6123{mm}-RAMESH KUMAR-SBIN0001234-HOUSE RENT {month}/{year}", "6123000000000020", Decimal("25000.00"), None),
        (date(year, month, 25), "UPI-SWIGGY-swiggy@axisbank-UPI", "0000412300000125", Decimal("640.00"), None),
        (last, f"NEFT CR-CITI0000002-NORTHWIND ANALYTICS PVT LTD-SALARY FOR {last:%b %Y}".upper(), f"CITIN{year}{mm}31", None, salary),
    ]
    return [{"date": d, "narration": n, "ref": r, "debit": dr, "credit": cr} for d, n, r, dr, cr in rows]


SALARY_CREDITS = {6: Decimal("102750.00"), 7: Decimal("102750.00"), 8: Decimal("107750.00"),
                  9: Decimal("104250.00")}
OD_INTEREST = {6: Decimal("1512.33"), 7: Decimal("1488.90"), 8: Decimal("1530.12"),
               9: Decimal("1497.41")}
OPENING = Decimal("145210.50")


def statement_rows(year=2026) -> list[dict]:
    rows, bal = [], OPENING
    for m in (6, 7, 8, 9):
        for r in _month_rows(year, m, SALARY_CREDITS[m], OD_INTEREST[m]):
            bal = bal + (r["credit"] or 0) - (r["debit"] or 0)
            rows.append({**r, "balance": bal})
    return rows


def _wrap(text: str, width: int = 34) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    while len(cur) > width:          # one long token: hard-wrap, as statements do
        lines.append(cur[:width])
        cur = cur[width:]
    lines.append(cur)
    return lines


COLS = {"date": 28, "narration": 70, "ref": 232, "value": 318, "debit": 418, "credit": 488,
        "balance": 568}


def _statement_header(page, y):
    _text(page, COLS["date"], y, "Date")
    _text(page, COLS["narration"], y, "Narration")
    _text(page, COLS["ref"], y, "Chq./Ref.No.")
    _text(page, COLS["value"], y, "Value Dt")
    _right(page, COLS["debit"], y, "Withdrawal Amt.")
    _right(page, COLS["credit"], y, "Deposit Amt.")
    _right(page, COLS["balance"], y, "Closing Balance")


def make_statement(path: pathlib.Path, start: date, end: date, rows_per_page: int = 14,
                   rows_all: list[dict] | None = None, opening_all: Decimal = OPENING,
                   title: str = "HDFC BANK-STYLE SAVINGS STATEMENT (FICTIONAL LAYOUT)",
                   account_no: str = "501000001234", holder: str = APPLICANT,
                   account_type: str = "Savings Account") -> None:
    rows_all = rows_all if rows_all is not None else statement_rows()
    rows = [r for r in rows_all if start <= r["date"] <= end]
    before = [r for r in rows_all if r["date"] < start]
    opening = before[-1]["balance"] if before else opening_all
    doc = pymupdf.open()
    pages = [rows[i:i + rows_per_page] for i in range(0, len(rows), rows_per_page)]
    for p_i, chunk in enumerate(pages):
        page = doc.new_page(width=595, height=842)
        _text(page, 28, 30, STAMP, 9)
        if p_i == 0:
            _text(page, 28, 52, title, 10)
            _text(page, 28, 70, f"Account Holder : {holder.upper()}    Account Type : {account_type}")
            _text(page, 28, 82, f"Account No : {account_no}")
            _text(page, 28, 94, f"Statement From : {start:%d/%m/%Y} To : {end:%d/%m/%Y}")
            y = 120
        else:
            y = 60
        _statement_header(page, y)
        y += 16
        for r in chunk:
            lines = _wrap(r["narration"])
            _text(page, COLS["date"], y, f"{r['date']:%d/%m/%y}")
            _text(page, COLS["narration"], y, lines[0])
            _text(page, COLS["ref"], y, r["ref"])
            _text(page, COLS["value"], y, f"{r['date']:%d/%m/%y}")
            if r["debit"]:
                _right(page, COLS["debit"], y, _money(r["debit"]))
            if r["credit"]:
                _right(page, COLS["credit"], y, _money(r["credit"]))
            _right(page, COLS["balance"], y, _money(r["balance"]))
            for extra in lines[1:]:
                y += 10
                _text(page, COLS["narration"], y, extra)
            y += 16
        _text(page, 28, 815, f"Page No .: {p_i + 1}")
    # summary on the last page
    page = doc[-1]
    y = 640
    debits = sum((r["debit"] or 0 for r in rows), Decimal(0))
    credits = sum((r["credit"] or 0 for r in rows), Decimal(0))
    closing = rows[-1]["balance"]
    _text(page, 28, y, "STATEMENT SUMMARY :-", 8)
    labels = [("Opening Balance", 80), ("Dr Count", 170), ("Cr Count", 240), ("Debits", 320),
              ("Credits", 410), ("Closing Bal", 510)]
    vals = [_money(opening), str(sum(1 for r in rows if r["debit"])),
            str(sum(1 for r in rows if r["credit"])), _money(debits), _money(credits), _money(closing)]
    for (label, xc), v in zip(labels, vals):
        lw = pymupdf.get_text_length(label, fontname=FONT, fontsize=7.5)
        _text(page, xc - lw / 2, y + 16, label)
        vw = pymupdf.get_text_length(v, fontname=FONT, fontsize=7.5)
        _text(page, xc - vw / 2, y + 30, v)
    doc.save(path)
    doc.close()


def make_od_statement(path: pathlib.Path) -> None:
    """An overdraft account statement: balances overdrawn, marked Dr."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    _text(page, 28, 30, STAMP, 9)
    _text(page, 28, 52, "AXIS BANK-STYLE OVERDRAFT STATEMENT (FICTIONAL LAYOUT)", 10)
    _text(page, 28, 82, "Account No : 922030004321")
    _text(page, 28, 94, "Statement From : 01/07/2026 To : 31/08/2026")
    _statement_header(page, 120)
    bal = Decimal("-180000.00")
    rows = [(date(2026, 7, 3), "TRF TO SB 501000001234", Decimal("20000.00"), None),
            (date(2026, 7, 31), "OD INT DR 01/07-31/07", Decimal("1488.90"), None),
            (date(2026, 8, 14), "NEFT CR-REPAYMENT", None, Decimal("15000.00")),
            (date(2026, 8, 31), "OD INT DR 01/08-31/08", Decimal("1530.12"), None)]
    y = 136
    opening = bal
    for d, n, dr, cr in rows:
        bal = bal + (cr or 0) - (dr or 0)
        _text(page, COLS["date"], y, f"{d:%d/%m/%y}")
        _text(page, COLS["narration"], y, n)
        if dr:
            _right(page, COLS["debit"], y, _money(dr))
        if cr:
            _right(page, COLS["credit"], y, _money(cr))
        _right(page, COLS["balance"], y, _money(abs(bal)) + ("Dr" if bal < 0 else "Cr"))
        y += 16
    _text(page, 28, 400, "STATEMENT SUMMARY :-", 8)
    for label, xc, v in (("Opening Balance", 80, _money(abs(opening)) + "Dr"),
                         ("Closing Bal", 510, _money(abs(bal)) + "Dr")):
        lw = pymupdf.get_text_length(label, fontname=FONT, fontsize=7.5)
        _text(page, xc - lw / 2, 416, label)
        vw = pymupdf.get_text_length(v, fontname=FONT, fontsize=7.5)
        _text(page, xc - vw / 2, 430, v)
    doc.save(path)
    doc.close()


# --- salary slips -----------------------------------------------------------------

SLIPS = {  # month: (basic, hra, special, arrears)
    7: (Decimal("50000"), Decimal("25000"), Decimal("50000"), Decimal("0")),
    8: (Decimal("50000"), Decimal("25000"), Decimal("50000"), Decimal("5000")),
    9: (Decimal("50000"), Decimal("25000"), Decimal("50000"), Decimal("0")),
}
DEDUCTIONS = (("Provident Fund", Decimal("7200")), ("Professional Tax", Decimal("200")),
              ("Income Tax (TDS)", Decimal("14850")))


def make_slip(path: pathlib.Path, month: int, year: int = 2026) -> None:
    basic, hra, special, arrears = SLIPS[month]
    gross = basic + hra + special + arrears
    ded = sum(v for _, v in DEDUCTIONS)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=420)
    _text(page, 40, 40, EMPLOYER, 13)
    _text(page, 40, 58, STAMP, 8)
    _text(page, 40, 80, f"Pay slip for the month of {date(year, month, 1):%B %Y}", 10)
    _text(page, 40, 100, f"Employee Name : {APPLICANT}")
    _text(page, 300, 100, "Employee ID : NW-00417")
    y = 130
    _text(page, 40, y, "Earnings", 9)
    _text(page, 300, y, "Deductions", 9)
    earnings = [("Basic", basic), ("House Rent Allowance", hra), ("Special Allowance", special)]
    if arrears:
        earnings.append(("Arrears", arrears))
    for i in range(max(len(earnings), len(DEDUCTIONS))):
        y += 14
        if i < len(earnings):
            _text(page, 40, y, earnings[i][0])
            _right(page, 260, y, _money(earnings[i][1]))
        if i < len(DEDUCTIONS):
            _text(page, 300, y, DEDUCTIONS[i][0])
            _right(page, 540, y, _money(DEDUCTIONS[i][1]))
    y += 22
    _text(page, 40, y, "Gross Earnings")
    _right(page, 260, y, _money(gross))
    _text(page, 300, y, "Total Deductions")
    _right(page, 540, y, _money(ded))
    y += 24
    _text(page, 40, y, "Net Pay")
    _right(page, 260, y, _money(gross - ded))
    doc.save(path)
    doc.close()


# --- credit report ---------------------------------------------------------------

ACCOUNTS = [
    dict(lender="BAJAJ FINANCE LTD", type="PERSONAL LOAN", own="INDIVIDUAL", num="XXXXXX5678",
         status="ACTIVE", opened="05-03-2025", closed="-", reported="31-08-2026",
         sanctioned="6,50,000", limit="-", balance="4,12,300", emi="18,450", overdue="0",
         dpd="000 000 000 000 000 000"),
    dict(lender="ICICI BANK", type="AUTO LOAN (PERSONAL)", own="INDIVIDUAL", num="XXXXXX9876",
         status="ACTIVE", opened="07-11-2024", closed="-", reported="31-08-2026",
         sanctioned="7,20,000", limit="-", balance="5,02,100", emi="-", overdue="0",
         dpd="000 000 000 000 000 000"),
    dict(lender="HDFC BANK", type="CREDIT CARD", own="INDIVIDUAL", num="XXXXXX9012",
         status="ACTIVE", opened="14-06-2019", closed="-", reported="31-08-2026",
         sanctioned="-", limit="2,00,000", balance="34,560", emi="-", overdue="0",
         dpd="000 000 000 000 000 000"),
    dict(lender="TATA CAPITAL LTD", type="OVERDRAFT", own="INDIVIDUAL", num="XXXXXX4321",
         status="ACTIVE", opened="02-01-2026", closed="-", reported="31-08-2026",
         sanctioned="5,00,000", limit="-", balance="1,85,000", emi="-", overdue="0",
         dpd="000 000 000 000 000 000"),
    dict(lender="HOME CREDIT INDIA", type="CONSUMER LOAN", own="JOINT", num="XXXXXX1111",
         status="CLOSED", opened="10-02-2023", closed="12-01-2025", reported="31-01-2025",
         sanctioned="45,000", limit="-", balance="0", emi="4,100", overdue="0",
         dpd="000 000 000"),
    dict(lender="AXIS BANK", type="PERSONAL LOAN", own="GUARANTOR", num="XXXXXX2222",
         status="ACTIVE", opened="20-08-2024", closed="-", reported="31-08-2026",
         sanctioned="3,00,000", limit="-", balance="1,10,000", emi="9,800", overdue="2,100",
         dpd="000 030 000 000 000 000"),
]
LABELS = [("MEMBER NAME", "lender"), ("ACCOUNT TYPE", "type"), ("OWNERSHIP", "own"),
          ("ACCOUNT NUMBER", "num"), ("ACCOUNT STATUS", "status"), ("DATE OPENED", "opened"),
          ("DATE CLOSED", "closed"), ("DATE REPORTED", "reported"),
          ("SANCTIONED AMOUNT", "sanctioned"), ("CREDIT LIMIT", "limit"),
          ("CURRENT BALANCE", "balance"), ("EMI AMOUNT", "emi"), ("AMOUNT OVERDUE", "overdue"),
          ("PAYMENT HISTORY (DPD)", "dpd")]
FIELD_FOR = {"lender": "lender", "type": "account_type", "own": "ownership", "num": "account_number",
             "status": "status", "opened": "date_opened", "closed": "date_closed",
             "reported": "last_reported", "sanctioned": "sanctioned_amount", "limit": "credit_limit",
             "balance": "current_balance", "emi": "emi", "overdue": "overdue", "dpd": "payment_history"}


def make_credit_report(path: pathlib.Path) -> dict:
    """Write the report; return the expected model response (with page refs)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    _text(page, 40, 40, STAMP, 9)
    _text(page, 40, 62, "CONSUMER CREDIT REPORT (FICTIONAL BUREAU FORMAT)", 12)
    _text(page, 40, 84, "BUREAU: SYNTHETIC CREDIT BUREAU")
    _text(page, 40, 96, "REPORT DATE: 28-09-2026")
    _text(page, 40, 108, f"NAME: {APPLICANT.upper()}")
    _text(page, 40, 130, "CREDIT SCORE: 772", 11)
    response = {
        "bureau": {"status": "present", "value": "SYNTHETIC CREDIT BUREAU",
                   "quote": "BUREAU: SYNTHETIC CREDIT BUREAU", "page": 1},
        "report_date": {"status": "present", "value": "28-09-2026",
                        "quote": "REPORT DATE: 28-09-2026", "page": 1},
        "score": {"status": "present", "value": "772", "quote": "CREDIT SCORE: 772", "page": 1},
        "applicant_name": {"status": "present", "value": APPLICANT.upper(),
                           "quote": f"NAME: {APPLICANT.upper()}", "page": 1},
        "accounts": [],
    }
    y, page_no = 160, 1
    for n, acc in enumerate(ACCOUNTS, 1):
        if y + 16 * (len(LABELS) + 2) > 800:
            page = doc.new_page(width=595, height=842)
            _text(page, 40, 40, STAMP, 9)
            page_no += 1
            y = 70
        _text(page, 40, y, f"ACCOUNT {n}", 10)
        y += 16
        out = {}
        for label, key in LABELS:
            line = f"{label}: {acc[key]}"
            _text(page, 52, y, line)
            y += 13
            blank = acc[key] == "-"
            out[FIELD_FOR[key]] = {"status": "blank" if blank else "present",
                                   "value": None if blank else acc[key], "quote": line,
                                   "page": page_no}
        out["drawing_power"] = {"status": "not_found"}
        response["accounts"].append(out)
        y += 14
    doc.save(path)
    doc.close()
    # Two deliberate model mistakes, so verification is exercised:
    # a wrong page number (verification relocates it and notes it) and a quote
    # that is nowhere in the document (kept, marked unverified).
    response["accounts"][3]["current_balance"]["page"] = 1
    response["accounts"][5]["overdue"]["quote"] = "AMOUNT OVERDUE: 2,100 (60+ DPD)"
    return response


# --- ITR JSON ------------------------------------------------------------------------

def itr_json() -> dict:
    return {"ITR": {"ITR1": {
        "CreationInfo": {"SWVersionNo": "1.0", "SWCreatedBy": "SYNTHETIC", "JSONCreatedBy": "SYNTHETIC",
                         "JSONCreationDate": "2026-07-20", "IntermediaryCity": "Delhi",
                         "Digest": "-"},
        "Form_ITR1": {"FormName": "ITR-1", "Description": STAMP, "AssessmentYear": "2026",
                      "SchemaVer": "Ver1.0", "FormVer": "Ver1.0"},
        "PersonalInfo": {"AssesseeName": {"FirstName": "ASHA", "SurNameOrOrgName": "VERMA (SYNTHETIC)"},
                         "PAN": "ZZZZZ0000Z"},
        "ITR1_IncomeDeductions": {"GrossSalary": 1500000, "Salary": 1500000,
                                  "DeductionUs16": 75000, "IncomeFromSal": 1425000,
                                  "GrossTotIncome": 1431200, "TotalIncome": 1431200},
        "ITR1_TaxComputation": {"TotalTaxPayable": 127860},
    }}}


# --- bundle ----------------------------------------------------------------------------

def build(directory: pathlib.Path) -> list[dict]:
    """Write every fixture into `directory`; return the manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    a = directory / "statement_jun_aug_2026.pdf"
    make_statement(a, date(2026, 6, 1), date(2026, 8, 31))
    b = directory / "statement_aug_sep_2026.pdf"
    make_statement(b, date(2026, 8, 1), date(2026, 9, 30))
    manifest += [{"file": a.name, "kind": "bank_statement"}, {"file": b.name, "kind": "bank_statement"}]
    for m in (7, 8, 9):
        p = directory / f"salary_slip_2026_{m:02d}.pdf"
        make_slip(p, m)
        manifest.append({"file": p.name, "kind": "salary_slip"})
    cr = directory / "credit_report_2026_09.pdf"
    response = make_credit_report(cr)
    manifest.append({"file": cr.name, "kind": "credit_report"})
    itr = directory / "itr_ay2026.json"
    itr.write_text(json.dumps(itr_json(), indent=1))
    manifest.append({"file": itr.name, "kind": "itr_json"})
    key = hashlib.sha256(cr.read_bytes()).hexdigest()
    (directory / "model_responses.json").write_text(
        json.dumps({f"{key}:record_credit_report": response}, indent=1))
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


# --- proprietorship ------------------------------------------------------------------

FIRM = "Verma Packaging Works (synthetic)"
OWNER = "Rohan Verma (synthetic)"


def current_account_rows() -> list[dict]:
    """12 months of a small packaging business: one dominant customer, cash sales,
    an ECS EMI with one return, and a large receipt followed by debits within two days."""
    rows, bal = [], Decimal("185000.00")
    for i in range(12):
        y, m = (2025 + (9 + i) // 12, (9 + i) % 12 + 1)          # Oct 2025 .. Sep 2026
        mm = f"{m:02d}"
        month = [
            (date(y, m, 3), f"NEFT CR-SBIN0004321-SHREE BALAJI TRADERS-INV{y}{mm}A", None, Decimal("185000.00")),
            (date(y, m, 5), "ACH D- TATA CAPITAL LTD-BL0098765", Decimal("21500.00"), None),
            (date(y, m, 8), "CASH DEPOSIT BY SELF - BRANCH 0412", None, Decimal("45000.00")),
            (date(y, m, 11), f"NEFT CR-ICIC0001111-KUMAR STORES-INV{y}{mm}", None, Decimal("38000.00")),
            (date(y, m, 14), f"NEFT DR-UTIB0002222-PRAKASH PAPER MILLS-PO{y}{mm}", Decimal("142000.00"), None),
            (date(y, m, 18), f"UPI-NEW LAXMI STORES-laxmi@okbank-UPI{y}{mm}18", None, Decimal("22500.00")),
            (date(y, m, 22), "GST PAYMENT CBDT-GSTN", Decimal("18400.00"), None),
            (date(y, m, 25), f"NEFT CR-SBIN0004321-SHREE BALAJI TRADERS-INV{y}{mm}B", None, Decimal("160000.00")),
            (date(y, m, 27), f"IMPS-77{mm}-ROHAN VERMA-HDFC0000001-TRF TO SB", Decimal("60000.00"), None),
            (date(y, m, 28), "ATM WDL 0412 SECTOR 18", Decimal("20000.00"), None),
            (date(y, m, 28), "SALARY TO STAFF NEFT BATCH", Decimal("95000.00"), None),
        ]
        if i == 4:
            month.insert(2, (date(y, m, 5), "ACH RTN TATA CAPITAL LTD-BL0098765 INSUFFICIENT FUNDS",
                             None, Decimal("21500.00")))
            month.insert(3, (date(y, m, 6), "ECS RETURN CHARGES INCL GST", Decimal("590.00"), None))
            month.insert(4, (date(y, m, 9), "ACH D- TATA CAPITAL LTD-BL0098765 REPRESENT", Decimal("21500.00"), None))
        if i == 9:
            month.append((date(y, m, 26), f"RTGS DR-YESB0003333-GANESH ENTERPRISES-ADV{y}{mm}",
                          Decimal("150000.00"), None))
        for d, n, dr, cr in sorted(month, key=lambda r: r[0]):
            bal = bal + (cr or 0) - (dr or 0)
            rows.append({"date": d, "narration": n, "ref": f"{d:%y%m%d}{len(rows):04d}",
                         "debit": dr, "credit": cr, "balance": bal})
    return rows


def gst_certificate(path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [(STAMP, 9), ("Government of India — Form GST REG-06 (fictional layout)", 11),
             ("Registration Certificate", 13), ("Registration Number : 27ZZZZZ0000Z1Z5", 9),
             (f"Legal Name : {OWNER.upper()}", 9), (f"Trade Name, if any : {FIRM.upper()}", 9),
             ("Constitution of Business : Proprietorship", 9),
             ("Address of Principal Place of Business : Plot 12, MIDC, Pune (synthetic)", 9),
             ("Date of Liability : 15/06/2022", 9),
             ("Period of Validity From : 15/06/2022 To : Not Applicable", 9),
             ("Type of Registration : Regular", 9), ("Date of issue of Certificate : 20/06/2022", 9)]
    y = 50
    for text, size in lines:
        _text(page, 40, y, text, size)
        y += 22
    doc.save(path)
    doc.close()


def udyam_certificate(path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [(STAMP, 9), ("Ministry of Micro, Small & Medium Enterprises (fictional layout)", 11),
             ("UDYAM REGISTRATION CERTIFICATE", 13), ("UDYAM REGISTRATION NUMBER UDYAM-MH-26-0000001", 9),
             (f"NAME OF ENTERPRISE {FIRM.upper()}", 9), ("TYPE OF ENTERPRISE MICRO", 9),
             ("MAJOR ACTIVITY MANUFACTURING", 9),
             ("DATE OF INCORPORATION / REGISTRATION OF ENTERPRISE 01/04/2022", 9),
             ("DATE OF COMMENCEMENT OF PRODUCTION/BUSINESS OPERATION 01/05/2022", 9),
             ("DATE OF UDYAM REGISTRATION 10/07/2022", 9)]
    y = 50
    for text, size in lines:
        _text(page, 40, y, text, size)
        y += 22
    doc.save(path)
    doc.close()


def build_proprietorship(directory: pathlib.Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    ca = directory / "current_account_oct25_sep26.pdf"
    rows = current_account_rows()
    make_statement(ca, date(2025, 10, 1), date(2026, 9, 30), rows_per_page=16, rows_all=rows,
                   opening_all=Decimal("185000.00"),
                   title="HDFC BANK-STYLE CURRENT ACCOUNT STATEMENT (FICTIONAL LAYOUT)",
                   account_no="502000009876", holder=FIRM, account_type="Current Account")
    sb = directory / "savings_account_apr_sep26.pdf"
    make_statement(sb, date(2026, 6, 1), date(2026, 9, 30), holder=OWNER)
    gst_certificate(directory / "gst_certificate.pdf")
    udyam_certificate(directory / "udyam_certificate.pdf")
    return []
