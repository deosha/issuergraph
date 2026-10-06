"""TransUnion CIBIL consumer report, as printed from myscore.cibil.com.

Deterministic. The print-out is a sequence of label/value rows read in visual
order (top to bottom, page by page): a value sits to the right of its label
on the same row, or on the row below. An account runs from "Member Name" to
the next "Member Name" and must carry an "Account Number"; the "OPEN
ACCOUNTS" / "CLOSED ACCOUNTS" heading above it gives its section. Enquiries
(which also print "Member Name") have no account number and are not accounts.
Browser furniture — the print timestamp row, the URL row — is skipped.

Every value is anchored to the label and value words it was read from; "-" is
stored as reported blank, never as zero.
"""
from __future__ import annotations

import re

from .bank import parse_date, row_text, visual_rows
from .extract import XFact, parse_amount_text
from .pages import AnchorError, Page, anchor_words

VERSION = "cibil-1"

# label → (field, kind); longest labels first so prefixes do not steal a row
LABELS = sorted({
    "Member Name": ("lender", "text"),
    "Account Type": ("account_type", "text"),
    "Account Number": ("account_number", "text"),
    "Ownership": ("ownership", "text"),
    "Credit Limit": ("credit_limit", "amount"),
    "Sanctioned Amount": ("sanctioned_amount", "amount"),
    "High Credit": ("high_credit", "amount"),
    "Current Balance": ("current_balance", "amount"),
    "Amount Overdue": ("overdue", "amount"),
    "Rate of Interest": ("rate_of_interest", "text"),
    "Repayment Tenure": ("repayment_tenure", "text"),
    "EMI Amount": ("emi", "amount"),
    "Payment Frequency": ("payment_frequency", "text"),
    "Actual Payment Amount": ("actual_payment", "amount"),
    "Date Opened / Disbursed": ("date_opened", "date"),
    "Date Closed": ("date_closed", "date"),
    "Date of Last Payment": ("last_payment", "date"),
    "Date Reported And Certified": ("last_reported", "date"),
    "Suit - Filed / Wilful Default": ("suit_filed", "text"),
    "Credit Facility Status": ("status", "text"),
    "Written-off Amount (Total)": ("written_off", "amount"),
    "Settlement Amount": ("settlement", "amount"),
}.items(), key=lambda kv: -len(kv[0]))
OTHER_LABELS = ("Cash Limit", "Value of Collateral", "Type of Collateral", "Written-off Amount (Principal)",
                "Payment Start Date", "Payment End Date", "Payment History", "PAYMENT STATUS",
                "ACCOUNT DETAILS", "Date Of Enquiry", "Enquiry Purpose", "Date Reported")
FURNITURE = (re.compile(r"^\d{2}/\d{2}/\d{4}, \d{2}:\d{2}\b"), re.compile(r"^https?://"),
             re.compile(r"^\d{1,3}/\d{1,3}$"))
SCORE_RE = re.compile(r"Your CIBIL Score is\s+(\S+)")


def is_cibil_print(pages: list[Page]) -> bool:
    head = " ".join(p.text for p in pages[:2])
    return "CIBIL Score" in head and "Member Name" in " ".join(p.text for p in pages)


def _rows(pages: list[Page]):
    """(page, words) for every non-furniture visual row, in reading order."""
    for p in pages:
        for r in visual_rows(p.words(), tol=2):
            t = row_text(r)
            if any(f.search(t) for f in FURNITURE):
                continue
            yield p, r


def _is_label(t: str) -> bool:
    return any(t.startswith(lbl) for lbl, _ in LABELS) or any(t.startswith(lbl) for lbl in OTHER_LABELS)


def _value(rows, i, label):
    """Value words for the label at rows[i]: rest of the row, else the next row."""
    page, r = rows[i]
    n = len(label.split())
    rest = r[n:]
    if rest:
        return page, r[:n], rest
    if i + 1 < len(rows):
        p2, r2 = rows[i + 1]
        if not _is_label(row_text(r2)):
            return p2, r[:n] if p2 is page else [], r2
    return page, r[:n], []


def _fact(field, kind, page, label_words, value_words) -> XFact | None:
    raw = " ".join(w.text for w in value_words).strip()
    if not raw:
        return None
    try:
        anchor = anchor_words(page, label_words + value_words)
    except AnchorError:
        return None
    if raw in ("-", "--", "NA"):
        return XFact(field, kind, None, "parser", True, anchor=anchor, page_no=page.page_no,
                     note="Reported blank (not zero).")
    if kind == "amount":
        value = parse_amount_text(raw)
    elif kind == "date":
        value = parse_date(raw)
    else:
        value = raw
    if value is None:
        return XFact(field, kind, None, "parser", False, quote=raw, page_no=page.page_no,
                     note=f"Printed value could not be read as {kind}.")
    return XFact(field, kind, value, "parser", True, anchor=anchor, page_no=page.page_no)


def parse(pages: list[Page]) -> tuple[list[XFact], list[list[XFact]]]:
    rows = list(_rows(pages))
    head: list[XFact] = []
    for page, r in rows[:40]:
        t = row_text(r)
        if t.startswith("CIBIL Score & Report") and not any(f.field == "bureau" for f in head):
            head.append(XFact("bureau", "text", "TransUnion CIBIL", "parser", True,
                              anchor=anchor_words(page, r[:1]), page_no=page.page_no,
                              note="Named in the report title."))
        if re.match(r"^Date\s*:", t) and not any(f.field == "report_date" for f in head):
            d = parse_date(r[-1].text)
            if d:
                head.append(XFact("report_date", "date", d, "parser", True,
                                  anchor=anchor_words(page, r), page_no=page.page_no))
        m = SCORE_RE.search(t)
        if m and not any(f.field in ("score", "score_code") for f in head):
            words = [w for w in r if w.text in ("Your", "CIBIL", "Score", "is", m.group(1))]
            if m.group(1).isdigit() and int(m.group(1)) > 5:
                head.append(XFact("score", "int", int(m.group(1)), "parser", True,
                                  anchor=anchor_words(page, words), page_no=page.page_no))
            else:
                head.append(XFact("score_code", "text", m.group(1), "parser", True,
                                  anchor=anchor_words(page, words), page_no=page.page_no,
                                  note="Bureau no-score code, not a score."))

    accounts: list[list[XFact]] = []
    section = None
    current: list[XFact] | None = None
    i = 0
    while i < len(rows):
        page, r = rows[i]
        t = row_text(r)
        if t in ("OPEN ACCOUNTS", "CLOSED ACCOUNTS"):
            section = (page, r)
        if t.startswith("ENQUIR") or t.startswith("Date Of Enquiry"):
            break
        if t.startswith("Member Name"):
            if current is not None:
                accounts.append(current)
            current = []
            if section:
                sp, sr = section
                current.append(XFact("section", "text", row_text(sr).title(), "parser", True,
                                     anchor=anchor_words(sp, sr), page_no=sp.page_no,
                                     note="From the section heading above the account."))
        if current is not None and t.startswith("Payment History"):
            hist, j = [], i + 1
            while j < len(rows) and rows[j][0] is page and not _is_label(row_text(rows[j][1])) \
                    and len(hist) < 24:
                hist.append(rows[j][1])
                j += 1
            if hist:
                words = r[:2] + [w for h in hist for w in h]
                current.append(XFact("payment_history", "text", " ".join(row_text(h) for h in hist),
                                     "parser", True, anchor=anchor_words(page, words), page_no=page.page_no,
                                     note="Month and days-past-due pairs as printed."))
        if current is not None:
            for label, (field, kind) in LABELS:
                if t.startswith(label) and not any(f.field == field for f in current):
                    p2, lw, vw = _value(rows, i, label)
                    f = _fact(field, kind, p2, lw, vw)
                    if f:
                        current.append(f)
                    break
        i += 1
    if current is not None:
        accounts.append(current)
    # An entry without an account number is not an account (e.g. an enquiry).
    accounts = [a for a in accounts if any(f.field == "account_number" for f in a)]
    for a in accounts:
        sec = next((f for f in a if f.field == "section"), None)
        st = next((f for f in a if f.field == "status"), None)
        if sec and sec.value.upper().startswith("CLOSED") and (st is None or st.value is None):
            a.append(XFact("status", "text", "Closed", "parser", True, anchor=sec.anchor,
                           page_no=sec.page_no, note="Listed under CLOSED ACCOUNTS."))
            if st is not None:
                a.remove(st)
    return head, accounts
