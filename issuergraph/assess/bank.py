"""Bank-statement extraction from a digital PDF's word geometry.

Deterministic and DB-free: pages in, rows + summary + issues out.

How a statement is read:
  * Every page is searched for its own column header (statements repeat it per
    page). A header is a visual row naming a date column, a balance column and
    either withdrawal/deposit columns or a single amount column.
  * Words are grouped into visual rows by their vertical centre — not by the
    PDF's text blocks, which in a table are usually one cell each.
  * A row whose date column holds a date starts a transaction; following rows
    without one are its wrapped narration. A footer or summary phrase ends the
    table on that page; the rest of the page is not read as transactions.
  * Amounts must carry two decimals (₹ statements always do), which is what
    keeps a reference number from being read as money. Balances may carry a
    Dr/Cr marker or a minus sign; overdrawn is stored as negative.
  * Each row is anchored to every word it was read from.

Checks: opening + credits − debits = closing, and row by row
previous balance + credit − debit = balance. Passing them says the rows were
read consistently; it says nothing about whether the document is authentic.

Layouts tested: HDFC-style netbanking PDFs (synthetic fixtures). Other banks
whose header uses the same vocabulary may parse; nothing else is claimed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .pages import AnchorError, Page, Word, anchor_span, anchor_words

VERSION = "bank-1"

AMOUNT_RE = re.compile(r"^-?[\d,]*\d\.\d{2}(?:Dr|Cr|DR|CR)?$")
DRCR = {"dr", "cr", "dr.", "cr."}
DATE_FORMATS = ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d-%b-%Y",
                "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y")

HEADER_WORDS = {
    "date": ("date", "txn date", "tran date", "transaction date", "post date"),
    "value_date": ("value dt", "value date"),
    "narration": ("narration", "description", "particulars", "remarks", "details",
                  "transaction details"),
    "ref": ("chq./ref.no.", "chq/ref no", "chq./ref.no", "ref no", "cheque no", "chq no",
            "reference", "chq / ref no.", "ref no./cheque no."),
    "debit": ("withdrawal amt.", "withdrawal", "withdrawals", "debit", "debits", "dr amount",
              "withdrawal amt"),
    "credit": ("deposit amt.", "deposit", "deposits", "credit", "credits", "cr amount",
               "deposit amt"),
    "amount": ("amount", "amount (inr)", "transaction amount"),
    "balance": ("closing balance", "balance", "balance (inr)", "running balance"),
}
STOP_PHRASES = ("statement summary", "opening balance", "page no", "this is a computer generated",
                "generated on", "end of statement", "contents of this statement",
                "registered office", "*closing balance includes", "state account branch")
BANKS = (("HDFC BANK", "HDFC Bank"), ("AXIS BANK", "Axis Bank"), ("ICICI BANK", "ICICI Bank"),
         ("KOTAK MAHINDRA", "Kotak Mahindra Bank"), ("STATE BANK OF INDIA", "SBI"),
         ("BANK OF BARODA", "Bank of Baroda"))


@dataclass
class Row:
    seq: int
    page_no: int
    txn_date: date
    value_date: date | None
    narration: str
    ref: str | None
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal | None
    anchor: dict


@dataclass
class Fact:
    field: str
    value_kind: str
    value: object
    anchor: dict | None
    note: str | None = None


@dataclass
class Statement:
    rows: list[Row] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    bank: str | None = None
    account_masked: str | None = None
    account_type: str | None = None
    period: tuple[date, date] | None = None


# --- tokens -------------------------------------------------------------------

def parse_date(text: str) -> date | None:
    t = text.strip().rstrip(",")
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if 1990 <= d.year <= 2100:
            return d
    return None


def parse_money(text: str) -> tuple[Decimal | None, str | None]:
    """(absolute amount, 'Dr'/'Cr'/'-' marker) or (None, None)."""
    t = text.strip()
    if not AMOUNT_RE.match(t):
        return None, None
    marker = None
    if t[-2:].lower() in ("dr", "cr"):
        marker, t = t[-2:].capitalize(), t[:-2]
    if t.startswith("-"):
        marker, t = "-", t[1:]
    return Decimal(t.replace(",", "")), marker


def visual_rows(words: list[Word], tol: float = 2.5) -> list[list[Word]]:
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(rows[-1][0].yc - w.yc) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.x0) for r in rows]


def row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


# --- header -------------------------------------------------------------------

@dataclass
class Column:
    kind: str
    x0: float
    x1: float


class ColumnList(list):
    """Columns of one header; `snapped` when aligned to drawn column lines."""
    snapped = False


def _header_columns(row: list[Word]) -> list[Column] | None:
    """Columns named by this row, or None if it is not a statement header."""
    cols: list[Column] = []
    i = 0
    while i < len(row):
        matched = None
        for n in (3, 2, 1):                     # longest phrase first
            if i + n > len(row):
                continue
            phrase = " ".join(w.text for w in row[i:i + n]).lower()
            for kind, names in HEADER_WORDS.items():
                if phrase in names:
                    matched = (kind, n)
                    break
            if matched:
                break
        if matched:
            kind, n = matched
            ws = row[i:i + n]
            if cols and cols[-1].kind == kind:
                cols[-1].x1 = ws[-1].x1
            else:
                cols.append(Column(kind, ws[0].x0, ws[-1].x1))
            i += n
        else:
            i += 1
    kinds = {c.kind for c in cols}
    if "date" in kinds and "balance" in kinds and (
            {"debit", "credit"} <= kinds or "amount" in kinds):
        return cols
    return None


def _assign(cols: list[Column], w: Word) -> str:
    money, _ = parse_money(w.text)
    amount_cols = [c for c in cols if c.kind in ("debit", "credit", "balance", "amount")]
    first_amount_x = min(c.x0 for c in amount_cols)
    if money is not None and w.x1 >= first_amount_x - 12:
        # Amounts are right-aligned under their header: nearest right edge.
        return min(amount_cols, key=lambda c: min(abs(w.x1 - c.x1), abs(w.xc - (c.x0 + c.x1) / 2))).kind
    if w.text.lower() in DRCR and w.x0 >= first_amount_x - 12:
        return "marker"
    inside = [c for c in cols if c.x0 - 2 <= w.xc <= c.x1 + 2 and c.kind not in
              ("debit", "credit", "balance", "amount")]
    if inside and getattr(cols, "snapped", False):
        return inside[0].kind
    # Text: the column whose span starts at or before the word's left edge.
    best = cols[0]
    for c in cols:
        if c.x0 - 4 <= w.x0:
            best = c
    if best.kind in ("debit", "credit", "balance", "amount"):
        return "narration_overflow"
    return best.kind


# --- statement ----------------------------------------------------------------

def _phrases(row: list[Word], gap: float = 6.0) -> list[list[Word]]:
    out: list[list[Word]] = []
    for w in row:
        if out and w.x0 - out[-1][-1].x1 <= gap:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _phrase_kind(text: str) -> str | None:
    t = text.lower()
    if "value" in t and ("dt" in t or "date" in t):
        return "value_date"
    if any(k in t for k in ("remark", "narration", "description", "particular", "details")):
        return "narration"
    if "withdraw" in t or "debit" in t:
        return "debit"
    if "deposit" in t or "credit" in t:
        return "credit"
    if "balance" in t:
        return "balance"
    if any(k in t for k in ("chq", "cheque", "ref")):
        return "ref"
    if "date" in t:
        return "date"
    if "amount" in t:
        return "amount"
    if t.replace(".", "").replace(" ", "") in ("sno", "slno", "srno", "#"):
        return "serial"
    return None


def _stacked_header(rows: list[list[Word]], i: int) -> tuple[list[Column], int] | None:
    """A header whose column names are stacked over two or three lines
    ("Transaction / Date", "Withdrawal / Amount (INR)"): phrases are grouped
    into columns by horizontal overlap, read top to bottom, then named."""
    for n in (2, 3):
        block = rows[i:i + n]
        if len(block) < n or any(b[0].yc - a[0].yc > 14 for a, b in zip(block, block[1:])):
            return None
        stacks: list[dict] = []
        for row in block:
            for ph in _phrases(row):
                x0, x1 = ph[0].x0, ph[-1].x1
                hit = next((s for s in stacks if x0 < s["x1"] and x1 > s["x0"]), None)
                text = " ".join(w.text for w in ph)
                if hit:
                    hit["text"] += " " + text
                    hit["x0"], hit["x1"] = min(hit["x0"], x0), max(hit["x1"], x1)
                else:
                    stacks.append({"x0": x0, "x1": x1, "text": text})
        cols = [Column(k, s["x0"], s["x1"]) for s in sorted(stacks, key=lambda s: s["x0"])
                if (k := _phrase_kind(s["text"]))]
        kinds = {c.kind for c in cols}
        if "date" in kinds and "balance" in kinds and ({"debit", "credit"} <= kinds or "amount" in kinds):
            return cols, n
    return None


def _rules(page: Page, path: str | None) -> tuple[list[float], list[float]]:
    """(horizontal rule ys, vertical rule xs) drawn on the page, if any."""
    if not path:
        return [], []
    import pymupdf

    doc = pymupdf.open(path)
    try:
        pg = doc[page.page_no - 1]
        hseg: dict[float, float] = {}       # y -> total drawn length (cells draw short segments)
        vs = set()
        for d in pg.get_drawings():
            for it in d["items"]:
                if it[0] == "l":
                    a, b = it[1], it[2]
                    if abs(a.y - b.y) < 0.5 and abs(a.x - b.x) > 5:
                        y = round(a.y)
                        hseg[y] = hseg.get(y, 0) + abs(a.x - b.x)
                    elif abs(a.x - b.x) < 0.5 and abs(a.y - b.y) > 10:
                        vs.add(round(a.x, 1))
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 1.5 and r.width > 5:
                        y = round(r.y0)
                        hseg[y] = hseg.get(y, 0) + r.width
                    elif r.width < 1.5 and r.height > 10:
                        vs.add(round(r.x0, 1))
        hs = {y for y, length in hseg.items() if length > page.width * 0.4}
        return sorted(hs), sorted(vs)
    finally:
        doc.close()


def _snap_to_rules(cols: list[Column], xs: list[float]) -> None:
    """With drawn column lines, a column is the space between the two lines
    around its header — data is aligned to the cell, not to the header text."""
    if len(xs) < 3:
        return
    for c in cols:
        mid = (c.x0 + c.x1) / 2
        left = [x for x in xs if x <= mid]
        right = [x for x in xs if x >= mid]
        if left and right:
            c.x0, c.x1 = left[-1], right[0]


def parse_statement(pages: list[Page], path: str | None = None) -> Statement:
    st = Statement()
    seq = 0
    current: dict | None = None
    header_pages = []

    def close():
        nonlocal current, seq
        if current is None:
            return
        cells = current
        current = None
        page = cells["page"]
        nar = " ".join(cells["narration"]).strip()
        debit = _sum(cells["debit"])
        credit = _sum(cells["credit"])
        if cells["amount"]:
            amt = _sum(cells["amount"])
            marker = cells["amount_marker"]
            if marker == "Dr":
                debit = amt
            elif marker == "Cr":
                credit = amt
            else:
                st.issues.append(f"Page {page.page_no}: row dated {cells['date']:%d %b %Y} has an "
                                 "amount without a Dr/Cr marker; not read.")
                return
        balance = None
        if cells["balance"]:
            b, marker = cells["balance"][-1]
            balance = -b if marker in ("Dr", "-") else b
        if debit is None and credit is None:
            st.issues.append(f"Page {page.page_no}: row dated {cells['date']:%d %b %Y} has no "
                             "withdrawal or deposit amount; not read.")
            return
        if debit is not None and credit is not None:
            st.issues.append(f"Page {page.page_no}: row dated {cells['date']:%d %b %Y} has both "
                             "a withdrawal and a deposit; kept, flagged.")
        try:
            anchor = anchor_words(page, cells["words"])
        except AnchorError as exc:
            st.issues.append(f"Page {page.page_no}: row could not be anchored ({exc}); not read.")
            return
        seq += 1
        st.rows.append(Row(seq, page.page_no, cells["date"], cells["value_date"], nar,
                           " ".join(cells["ref"]) or None, debit, credit, balance, anchor))

    for page in pages:
        rows = visual_rows(page.words())
        hrules, vrules = _rules(page, path)
        cols = None
        header_y = None
        last_y = None
        stopped = False
        banded = False
        skip = 0
        r_i = -1
        while r_i + 1 < len(rows):
            r_i += 1
            row = rows[r_i]
            if skip:
                skip -= 1
                continue
            text_l = row_text(row).lower()
            maybe = _header_columns(row)
            used = 1
            if not maybe:
                stacked = _stacked_header(rows, r_i)
                if stacked:
                    maybe, used = stacked
            if maybe:
                close()
                cols, stopped = ColumnList(maybe), False
                header_y = max(w.y1 for r in rows[r_i:r_i + used] for w in r)
                if len(vrules) >= 3:
                    _snap_to_rules(cols, vrules)
                    cols.snapped = True
                header_pages.append(page.page_no)
                last_y = rows[r_i + used - 1][0].yc
                below = [y for y in hrules if y > header_y]
                if len(below) >= 3:
                    # Ruled table: each band between two rules is one row, so a
                    # remark wrapped above and below its date stays with it.
                    banded = True
                    rest = [w for r in rows[r_i + used:] for w in r]
                    bands = []
                    for a, b in zip(below, below[1:]):
                        band = sorted((w for w in rest if a < w.yc < b), key=lambda w: (round(w.yc), w.x0))
                        if band:
                            bands.append(band)
                    after = [w for w in rest if w.yc > below[-1]]
                    rows = rows[:r_i + used] + bands + visual_rows(after)
                skip = used - 1
                continue
            if cols is None or stopped:
                continue
            if any(p in text_l for p in STOP_PHRASES):
                close()
                stopped = True
                continue
            cells = {k: [] for k in ("date", "value_date", "narration", "ref", "debit", "credit",
                                     "balance", "amount", "narration_overflow", "serial")}
            marker = None
            for w in row:
                kind = _assign(cols, w)
                if kind == "marker":
                    marker = w.text.capitalize().rstrip(".")
                    continue
                cells[kind].append(w)
            d = parse_date(" ".join(w.text for w in cells["date"])) if cells["date"] else None
            if d is not None:
                close()
                current = {"page": page, "date": d, "value_date": None, "narration": [], "ref": [],
                           "debit": [], "credit": [], "amount": [], "amount_marker": None,
                           "balance": [], "words": []}
            elif current is None or current["page"] is not page:
                continue
            elif banded:
                close()          # in a ruled table a band without a date is not a continuation
                continue
            elif last_y is not None and row[0].yc - last_y > 40:
                close()          # a large gap: whatever follows is not this table
                stopped = True
                continue
            last_y = row[0].yc
            vd = parse_date(" ".join(w.text for w in cells["value_date"])) if cells["value_date"] else None
            if vd and current["value_date"] is None:
                current["value_date"] = vd
            current["narration"].extend(w.text for w in cells["narration"] + cells["narration_overflow"])
            current["ref"].extend(w.text for w in cells["ref"])
            for kind in ("debit", "credit", "amount"):
                for w in cells[kind]:
                    current[kind].append(parse_money(w.text))
            if cells["amount"] and marker:
                current["amount_marker"] = marker
            for w in cells["balance"]:
                b, m = parse_money(w.text)
                current["balance"].append((b, marker if marker and not m else m))
            current["words"].extend(w for w in row)
        close()

    if not header_pages:
        st.issues.append("No transaction-table header was recognised on any page. This layout "
                         "is not supported by the statement parser.")
    _metadata(pages, st)
    _summary(pages, st)
    _validate(st)
    return st


def _sum(values: list) -> Decimal | None:
    nums = [v for v, _ in values if v is not None]
    if not nums:
        return None
    if len(nums) > 1:
        return None     # two amounts in one cell is not something we guess about
    return nums[0]


# --- metadata -------------------------------------------------------------------

PERIOD_RE = re.compile(
    r"(?:From|Period)\s*:?\s*(\d{1,2}[/\-.][\w]{2,3}[/\-.]\d{2,4})\s*(?:To|to|-)\s*:?\s*"
    r"(\d{1,2}[/\-.][\w]{2,3}[/\-.]\d{2,4})")
ACCOUNT_RE = re.compile(r"(?i)(?:Account\s*(?:No|Number)|A/C\s*No)\.?\s*:?\s*([0-9Xx*]{6,20})")
ACCOUNT_TYPE_RE = re.compile(r"(?i)\b(current account|current a/c|savings account|saving account|"
                             r"savings a/c|overdraft account|od account|cash credit)\b")
PERIOD_WORDS_RE = re.compile(
    r"(?i)period\s*:?\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*(?:-|to)\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")


def _metadata(pages: list[Page], st: Statement) -> None:
    if not pages:
        return
    first = pages[0]
    upper = first.text.upper()
    for needle, name in BANKS:
        i = upper.find(needle)
        if i >= 0:
            st.bank = name
            st.facts.append(Fact("bank", "text", name, anchor_span(first, i, i + len(needle))))
            break
    m = PERIOD_RE.search(first.text) or PERIOD_WORDS_RE.search(first.text)
    if m:
        a, b = parse_date(m.group(1)), parse_date(m.group(2))
        if a and b:
            st.period = (a, b)
            st.facts.append(Fact("period_start", "date", a, anchor_span(first, m.start(1), m.end(1))))
            st.facts.append(Fact("period_end", "date", b, anchor_span(first, m.start(2), m.end(2))))
    m = ACCOUNT_TYPE_RE.search(first.text)
    if m:
        word = m.group(1).lower()
        st.account_type = ("current" if "current" in word else "savings" if "saving" in word
                           else "cash credit" if "cash" in word else "overdraft")
        st.facts.append(Fact("account_type", "text", st.account_type, anchor_span(first, m.start(1), m.end(1)),
                             note="As named on the statement."))
    m = ACCOUNT_RE.search(first.text)
    if m:
        digits = m.group(1)
        st.account_masked = "XX" + digits[-4:]
        st.facts.append(Fact("account_number", "text", st.account_masked,
                             anchor_span(first, m.start(1), m.end(1)),
                             note="Shown masked to the last four digits."))


SUMMARY_LABELS = {
    "opening_balance": ("opening balance",),
    "closing_balance": ("closing bal", "closing balance"),
    "total_debits": ("debits", "total debits", "total withdrawals"),
    "total_credits": ("credits", "total credits", "total deposits"),
    "debit_count": ("dr count",),
    "credit_count": ("cr count",),
}


def _summary(pages: list[Page], st: Statement) -> None:
    """The statement's own summary: a label row with a value row under it."""
    for page in pages:
        rows = visual_rows(page.words())
        for i, row in enumerate(rows[:-1]):
            t = row_text(row).lower()
            if "opening" not in t or "closing" not in t:
                continue
            labels = _label_positions(row)
            values = [w for w in rows[i + 1] if re.match(r"^-?[\d,]+(?:\.\d{2})?(?:Dr|Cr)?$", w.text)]
            if len(labels) < 2 or not values:
                continue
            for key, xc in labels.items():
                w = min(values, key=lambda v: abs(v.xc - xc))
                if abs(w.xc - xc) > 80:
                    continue
                if key.endswith("_count"):
                    value = int(w.text.replace(",", "")) if w.text.replace(",", "").isdigit() else None
                    kind = "int"
                else:
                    amt, marker = parse_money(w.text)
                    value = None if amt is None else (-amt if marker in ("Dr", "-") else amt)
                    kind = "amount"
                if value is not None:
                    st.facts.append(Fact(key, kind, value, anchor_words(page, [w]),
                                         note="As stated in the statement summary."))
            return


def _label_positions(row: list[Word]) -> dict[str, float]:
    found: dict[str, float] = {}
    i = 0
    while i < len(row):
        hit = None
        for n in (2, 1):
            phrase = " ".join(w.text for w in row[i:i + n]).lower().rstrip(":")
            for key, names in SUMMARY_LABELS.items():
                if phrase in names and key not in found:
                    hit = (key, n)
                    break
            if hit:
                break
        if hit:
            ws = row[i:i + hit[1]]
            found[hit[0]] = (ws[0].x0 + ws[-1].x1) / 2
            i += hit[1]
        else:
            i += 1
    return found


# --- validation -----------------------------------------------------------------

def _fact(st: Statement, name: str):
    return next((f.value for f in st.facts if f.field == name), None)


def _validate(st: Statement) -> None:
    rows = st.rows
    s = st.summary
    s["rows"] = len(rows)
    s["total_debits"] = str(sum((r.debit or 0 for r in rows), Decimal(0)))
    s["total_credits"] = str(sum((r.credit or 0 for r in rows), Decimal(0)))
    s["debit_count"] = sum(1 for r in rows if r.debit)
    s["credit_count"] = sum(1 for r in rows if r.credit)
    if rows:
        s["first_date"] = rows[0].txn_date.isoformat()
        s["last_date"] = rows[-1].txn_date.isoformat()

    opening = _fact(st, "opening_balance")
    closing = _fact(st, "closing_balance")
    s["opening_basis"] = "reported"
    if opening is None and rows and rows[0].balance is not None:
        r0 = rows[0]
        opening = r0.balance - (r0.credit or 0) + (r0.debit or 0)
        s["opening_basis"] = "derived from the first row's balance"
    if closing is None and rows and rows[-1].balance is not None:
        closing = rows[-1].balance
        s["closing_basis"] = "last row's balance (no closing balance stated)"
    else:
        s["closing_basis"] = "reported"
    s["opening_balance"] = None if opening is None else str(opening)
    s["closing_balance"] = None if closing is None else str(closing)

    if opening is not None and closing is not None:
        expected = opening + Decimal(s["total_credits"]) - Decimal(s["total_debits"])
        s["recon_expected_closing"] = str(expected)
        s["recon_difference"] = str(closing - expected)
        s["recon_ok"] = abs(closing - expected) < Decimal("0.01")
    else:
        s["recon_ok"] = None
        st.issues.append("Opening or closing balance not found: the balance identity cannot be "
                         "checked.")

    breaks = []
    prev = opening
    for r in rows:
        if r.balance is None:
            continue
        if prev is not None:
            exp = prev + (r.credit or 0) - (r.debit or 0)
            if abs(exp - r.balance) >= Decimal("0.01"):
                breaks.append({"seq": r.seq, "page_no": r.page_no, "date": r.txn_date.isoformat(),
                               "expected": str(exp), "stated": str(r.balance)})
        prev = r.balance
    s["running_balance_rows"] = sum(1 for r in rows if r.balance is not None)
    s["running_breaks"] = breaks

    for key in ("total_debits", "total_credits"):
        stated = _fact(st, key)
        if stated is not None and Decimal(str(stated)) != Decimal(s[key]):
            st.issues.append(f"Statement summary {key.replace('_', ' ')} {stated} differs from the "
                             f"sum of rows read ({s[key]}).")
    for key in ("debit_count", "credit_count"):
        stated = _fact(st, key)
        if stated is not None and stated != s[key]:
            st.issues.append(f"Statement summary {key.replace('_', ' ')} {stated} differs from "
                             f"rows read ({s[key]}).")
    if s["recon_ok"] is False:
        st.issues.append(f"Balance identity fails by {s['recon_difference']}: opening + credits − "
                         "debits does not equal closing.")
    if breaks:
        st.issues.append(f"Running balance breaks at {len(breaks)} row(s).")
    if st.period and rows:
        out = [r for r in rows if not (st.period[0] <= r.txn_date <= st.period[1])]
        if out:
            st.issues.append(f"{len(out)} row(s) dated outside the stated period.")
