"""Excel workbook for a case, built from `analysis.build` — the same dict the
UI renders — so every total in the workbook is the total on screen.

Totals are written as values (what the UI showed) with a SUM formula beside
them as a check that recalculates in Excel.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "1E3A5F"
TEAL = "0F766E"
HEAD = PatternFill("solid", fgColor=NAVY)
BAND = PatternFill("solid", fgColor="EEF4F8")
WARN = PatternFill("solid", fgColor="FFF7E6")
THIN = Border(bottom=Side(style="thin", color="D5DEE8"))
MONEY = '#,##0.00;[Red]-#,##0.00'
ORIGIN = {"reported": "Reported", "unverified": "Unverified (model)", "analyst": "Analyst",
          "inferred": "Inferred", "calculated": "Calculated", "suggested": "Suggested", None: ""}


def _title(ws, text, sub=None):
    ws["A1"] = text
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    if sub:
        ws["A2"] = sub
        ws["A2"].font = Font(italic=True, color="555555")
    return 4


def _table(ws, row, headers, rows, money_cols=(), widths=None, title=None):
    if title:
        ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=11, color=TEAL)
        row += 1
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    first = row + 1
    for r_i, values in enumerate(rows):
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=first + r_i, column=c, value=v)
            cell.border = THIN
            cell.alignment = Alignment(vertical="top", wrap_text=isinstance(v, str) and len(v) > 40)
            if c - 1 in money_cols:
                cell.number_format = MONEY
    if widths:
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = w
    return first, first + len(rows) - 1, first + len(rows) + 1


def _v(view):
    """A fact view's display value."""
    if not view:
        return "not reported"
    if view.get("reported_blank"):
        return "reported blank"
    return view.get("value")


def _src(view, docs):
    if not view:
        return ""
    d = docs.get(view["document_id"], {})
    loc = f"p.{view['page_no']}" if view.get("page_no") else view.get("json_path") or ""
    return f"{d.get('filename', '')} {loc}".strip()


def workbook(out: dict) -> Workbook:
    wb = Workbook()
    docs = {d["id"]: d for d in out["documents"]}
    case = out["case"]
    elig = out["eligibility"]

    # --- Summary -----------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    r = _title(ws, f"IssuerGraph Assess — {case['label']}",
               "Prepared from applicant-supplied documents. " + elig["label"])
    info = [("Applicant", case.get("applicant_name")), ("Borrower type", case.get("borrower_type")),
            ("Requested facility", case.get("requested_product")),
            ("Requested amount", case.get("requested_amount")),
            ("Synthetic case", "YES — fictional data" if case.get("is_synthetic") else "No"),
            ("Workbook generated", out["generated_at"]),
            ("Documents", len(out["documents"])),
            ("Documents complete", sum(1 for d in out["documents"] if d["status"] == "complete")),
            ("Open review items", sum(1 for i in out["review"] if not i["resolved"])),
            ("Open blocking items", elig.get("open_blocking")),
            ("Included monthly obligations", out["obligations"]["total_included"]),
            ("Unresolved liability rows", out["obligations"]["unresolved"]),
            ("Eligibility status", elig["status"].replace("_", " ")),
            ("Illustrative EMI capacity", elig.get("capacity_emi")),
            ("Illustrative principal", elig.get("principal"))]
    for k, v in info:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=r, column=2, value=v)
        if isinstance(v, float) and k not in ("Documents",):
            c.number_format = MONEY
        r += 1
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 60

    # --- Documents --------------------------------------------------------------------
    ws = wb.create_sheet("Documents")
    r = _title(ws, "Documents received", "Status is the extraction outcome; 'complete' means every "
               "declared field was read and verified, not that the document is authentic.")
    _table(ws, r, ["Doc ID", "File", "Type", "Status", "Reasons", "Source", "Document date",
                   "Period from", "Period to", "Pages", "OCR pages", "Model", "Model calls",
                   "Input tokens", "Output tokens", "Processed at"],
           [[d["id"], d["filename"], d["kind"], d["status"], " ".join(d["status_reasons"] or []),
             d["source_label"], d["doc_date"], d["period_start"], d["period_end"], d["page_count"],
             d["ocr_pages"], d["model_id"], d["model_calls"], d["input_tokens"], d["output_tokens"],
             d["processed_at"]] for d in out["documents"]],
           widths=[7, 34, 15, 11, 50, 22, 13, 12, 12, 7, 9, 26, 8, 10, 10, 26])
    r = _table(ws, r + len(out["documents"]) + 3, ["Checklist item", "Received", "Missing"],
               [[c["item"], c["received"], c["missing"]] for c in out["checklist"]["rows"]],
               title=out["checklist"]["note"])[2]

    # --- Income ---------------------------------------------------------------------------
    ws = wb.create_sheet("Income")
    inc = out["income"]
    r = _title(ws, "Income", "Gross pay, net pay, taxable income and bank credits are different "
               "figures and are kept apart.")
    _, _, r = _table(ws, r, ["Month", "Employer", "Gross pay", "Total deductions", "Net pay",
                             "Gross−deductions=net?", "Source"],
                     [[s["month"], s["employer"], s["gross"], s["deductions"], s["net"],
                       ("yes" if s["arith"]["ok"] else f"no ({s['arith']['difference']})") if s["arith"] else "",
                       _src(s["fields"].get("net_pay"), docs)] for s in inc["slips"]],
                     money_cols=(2, 3, 4), widths=[12, 30, 14, 16, 14, 18, 40], title="Salary slips")
    _, _, r = _table(ws, r, ["Month", "Slip net pay", "Bank salary credit(s)", "Difference", "Credit dates",
                             "Assessment"],
                     [[c["label"], c["slip_net"], c["bank_total"], c["difference"],
                       ", ".join(b["date"] for b in c["bank_credits"]), c["note"]] for c in inc["comparison"]],
                     money_cols=(1, 2, 3), title="Slip vs bank, by month (" + inc["salary_credit_rule"] + ")")
    for i in inc["itr"]:
        f = i["fields"]
        _, _, r = _table(ws, r, ["Figure", "Value", "JSON path"],
                         [[k.replace("_", " "), _v(f.get(k)), (f.get(k) or {}).get("json_path")]
                          for k in ("assessment_year", "gross_salary", "income_from_salary",
                                    "gross_total_income", "total_income", "tax_payable", "employer")
                          if f.get(k)] + [["filing status", (i["summary"] or {}).get("filed_status"), ""]],
                         money_cols=(1,), title="Income-tax return (JSON)")
    if inc["annualised_gross"]:
        ws.cell(row=r, column=1, value="Annualised gross (ASSUMPTION)").font = Font(bold=True)
        ws.cell(row=r, column=2, value=inc["annualised_gross"]["value"]).number_format = MONEY
        ws.cell(row=r, column=3, value=inc["annualised_gross"]["basis"])

    # --- Liabilities ----------------------------------------------------------------------
    ws = wb.create_sheet("Liabilities")
    ob = out["obligations"]
    r = _title(ws, "Liability schedule", "One row per obligation. A bureau account and its matching "
               "bank repayment are one row. An OD limit is not debt and has no fixed EMI.")
    rows = ob["rows"]
    first, last, r = _table(
        ws, r, ["Account / source", "Type", "Ownership", "Sanctioned", "Limit", "Outstanding",
                "Bureau EMI", "Observed bank debit", "Included?", "Monthly obligation", "Origin", "Basis",
                "Unresolved"],
        [[x["label"], x["account_type"] or x["kind"], x["ownership"], x["sanctioned"], x["limit"],
          x["outstanding"], x["bureau_emi"] if x["bureau_emi"] is not None else x["bureau_emi_state"],
          (x["observed"] or {}).get("amount"), "yes" if x["included"] else "no", x["amount"],
          ORIGIN.get(x["origin"], x["origin"]), x["basis"], x["unresolved"]] for x in rows],
        money_cols=(3, 4, 5, 6, 7, 9), widths=[38, 20, 12, 13, 13, 13, 14, 14, 9, 14, 11, 50, 50])
    ws.cell(row=r, column=9, value="Total included").font = Font(bold=True)
    tot = ws.cell(row=r, column=10, value=ob["total_included"])
    tot.number_format = MONEY
    tot.font = Font(bold=True)
    ws.cell(row=r + 1, column=9, value="Check (formula)")
    if rows:
        chk = ws.cell(row=r + 1, column=10,
                      value=f'=SUMIF(I{first}:I{last},"yes",J{first}:J{last})')
        chk.number_format = MONEY
    for i, x in enumerate(rows):
        if x["unresolved"]:
            for c in range(1, 14):
                ws.cell(row=first + i, column=c).fill = WARN
    r += 3
    tl_rows = []
    for t in out["credit"]["tradelines"]:
        f = t["fields"]
        tl_rows.append([t["lender"], t["account_type"], t["ownership"], t["status"],
                        _v(f.get("date_opened")), _v(f.get("date_closed")), _v(f.get("last_reported")),
                        _v(f.get("sanctioned_amount")), _v(f.get("credit_limit")),
                        _v(f.get("current_balance")), _v(f.get("emi")), _v(f.get("overdue")),
                        _v(f.get("payment_history")),
                        "; ".join(f"{k}: {v['status']}" for k, v in f.items() if v["status"] != "reported"),
                        _src(f.get("current_balance") or f.get("lender"), docs)])
    rep = out["credit"]["reports"]
    hdr = ""
    if rep:
        h = rep[0]["fields"]
        hdr = (f"{_v(h.get('bureau'))} · report date {_v(h.get('report_date'))} · score "
               f"{_v(h.get('score')) if h.get('score') else _v(h.get('score_code'))} · {rep[0]['supplied_by']}")
    _table(ws, r, ["Lender", "Account type", "Ownership", "Status", "Opened", "Closed", "Last reported",
                   "Sanctioned", "Credit limit", "Current balance", "EMI", "Overdue", "Payment history",
                   "Non-reported fields", "Source"], tl_rows, money_cols=(7, 8, 9, 10, 11),
           title="Credit report accounts — " + hdr)

    # --- Bank ------------------------------------------------------------------------------
    ws = wb.create_sheet("Bank")
    bk = out["banking"]
    r = _title(ws, "Bank statements and reconciliation", "A statement that reconciles was read "
               "consistently; reconciliation is not proof that it is authentic.")
    _, _, r = _table(ws, r, ["Doc ID", "File", "Bank", "Account", "Period from", "Period to", "Opening",
                             "Opening basis", "Credits", "Debits", "Closing", "Opening+Cr−Dr=Closing",
                             "Running-balance breaks", "Rows", "Duplicate rows (overlap)", "Issues"],
                     [[s["document_id"], s["filename"], s["bank"], s["account"], s["period_start"],
                       s["period_end"], _f(s["summary"].get("opening_balance")), s["summary"].get("opening_basis"),
                       _f(s["summary"].get("total_credits")), _f(s["summary"].get("total_debits")),
                       _f(s["summary"].get("closing_balance")),
                       {True: "passes", False: "FAILS", None: "not checked"}[s["summary"].get("recon_ok")],
                       len(s["summary"].get("running_breaks") or []), s["summary"].get("rows"),
                       s["duplicates"], " ".join(s["summary"].get("issues") or [])] for s in bk["statements"]],
                     money_cols=(6, 8, 9, 10), widths=[7, 30, 12, 10, 12, 12, 14, 18, 14, 14, 14, 16, 12, 7, 12, 40])
    _, _, r = _table(ws, r, ["Account", "From", "To", "Unique rows", "Duplicate rows", "Credits (unique)",
                             "Debits (unique)", "Gaps", "Overlaps"],
                     [[f"{a['bank']} {a['account']}", a["from"], a["to"], a["unique_rows"], a["duplicate_rows"],
                       a["total_credits"], a["total_debits"],
                       "; ".join(f"{g['from']}–{g['to']}" for g in a["gaps"]),
                       "; ".join(f"{o['from']}–{o['to']}" for o in a["overlaps"])] for a in bk["accounts"]],
                     money_cols=(5, 6), title="Accounts (overlapping statements de-duplicated)")
    _, _, r = _table(ws, r, ["Recurring debit", "Category", "Typical amount", "Fixed?", "Months", "Count"],
                     [[s["signature"], s["category"], s["amount"], "yes" if s["fixed_amount"] else
                       f"varies {s['min']}–{s['max']}", ", ".join(s["months"]), s["count"]] for s in bk["series"]],
                     money_cols=(2,), title="Candidate servicing debits (inferred)")
    tl = {t["key"]: t for t in out["credit"]["tradelines"]}
    _table(ws, r, ["Bureau account", "Bank debit series", "Signals", "Strength", "Status", "Analyst reason"],
           [[tl[m["tradeline"]]["label"], m["series"].split("|")[-1], ", ".join(m["signals"]), m["strength"],
             m["status"], m.get("reason")] for m in out["matches"]], title="Bureau ↔ bank matches")

    # --- Business & banking analysis --------------------------------------------------------
    ws = wb.create_sheet("Business & Banking")
    r = _title(ws, "Business profile and banking analysis",
               "Computed from parsed, de-duplicated statement rows. Review prompts are not lender rules.")
    biz = out.get("business") or {}
    v = biz.get("vintage") or {}
    if out["case"].get("borrower_type") != "salaried":
        _, _, r = _table(ws, r, ["Item", "Value", "Basis / source"],
                         [["Business vintage (years)", v.get("years"), v.get("basis")],
                          ["Since", v.get("since"), _src(v.get("fact"), docs)],
                          ["Current account statement", "yes" if biz.get("has_current_account") else "no",
                           ", ".join(biz.get("account_types") or [])]] +
                         [[f"{'GST' if g['kind'] == 'gst_certificate' else 'Udyam'}: {k.replace('_', ' ')}", _v(fv),
                           _src(fv, docs)] for g in biz.get("registrations", []) for k, fv in g["fields"].items()],
                         widths=[40, 22, 60], title="Business profile")
    for a in out["banking"].get("analysis", []):
        name = f"{a['account_key'].replace('|', ' ')} ({a['account_type'] or 'type not stated'})"
        top = a["concentration"]["top"]
        _, _, r = _table(ws, r, ["Metric", "Value", "Basis"],
                         [["Period", f"{a['from']} to {a['to']}", f"{a['month_count']} months"],
                          ["Average monthly balance", a["amb"], a["amb_method"]],
                          ["Business credits (total)", a["business_credits"], "Excludes returns and own-account transfers"],
                          ["Business credits per month", a["avg_monthly_business_credits"], ""],
                          ["Cash deposits", a["cash_deposits"]["amount"], f"{a['cash_deposits']['count']} entries"],
                          ["Cash withdrawals", a["cash_withdrawals"]["amount"], f"{a['cash_withdrawals']['count']} entries"],
                          ["Returns / bounces", a["returns"]["count"], "; ".join(f"{x['date']} {x['narration']}" for x in a["returns"]["rows"])],
                          ["Days below zero", a["negative_days"], ""],
                          ["Credits matched by subsequent debits within 2 days (share of large credits)", a["retention"]["share"], a["retention"]["rule"]],
                          ["Largest single payer (share)", top[0]["share"] if top else None, top[0]["party"] if top else ""]],
                         money_cols=(1,), title=name)
        _, _, r = _table(ws, r, ["Month", "AMB", "Business credits", "Credits (n)", "Debits", "Debits (n)", "Cash in",
                                 "Cash out", "Returns", "Min balance"],
                         [[m["month"], m["amb"], m["business_credits"], m["credit_n"], m["debits"], m["debit_n"],
                           m["cash_dep"], m["cash_wdl"], m["returns"], m["min_balance"]] for m in a["months"]],
                         money_cols=(1, 2, 4, 6, 7, 9), title="Monthly pattern")
        _, _, r = _table(ws, r, ["Payer (inferred from narration)", "Share", "Amount", "Credits"],
                         [[t["party"], t["share"], t["amount"], t["count"]] for t in top],
                         money_cols=(2,), title="Who pays in")

    # --- Transactions ----------------------------------------------------------------------
    ws = wb.create_sheet("Transactions")
    r = _title(ws, "Transactions", "Rows repeated in an overlapping statement are listed and marked "
               "duplicate; they are excluded from every total.")
    _table(ws, r, ["Account", "Date", "Narration", "Ref", "Debit", "Credit", "Balance", "Category",
                   "Category origin", "Basis", "Duplicate?", "Source"],
           [[t["account_key"], t["txn_date"], t["narration"], t["ref"], t["debit"], t["credit"], t["balance"],
             t["category"], t["category_origin"], t["category_basis"], "duplicate" if t["duplicate"] else "",
             f"{docs[t['document_id']]['filename']} p.{t['page_no']}"] for t in bk["transactions"]],
           money_cols=(4, 5, 6), widths=[18, 11, 50, 18, 12, 12, 13, 14, 12, 40, 10, 36])

    # --- Findings ----------------------------------------------------------------------------
    ws = wb.create_sheet("Findings")
    r = _title(ws, "Findings and review items")
    _table(ws, r, ["Area", "Item", "Blocks the result?", "Status", "Resolution"],
           [[i["area"], i["text"], "yes" if i["blocking"] else "no",
             "resolved" if i["resolved"] else "open", i["resolution"]] for i in out["review"]],
           widths=[14, 90, 14, 10, 40])

    # --- Eligibility ----------------------------------------------------------------------------
    ws = wb.create_sheet("Eligibility")
    r = _title(ws, "Illustrative eligibility worksheet", elig["label"])
    ws["A3"] = elig["label"]
    ws["A3"].font = Font(bold=True, color="9A3412")
    ins = elig["inputs"]

    def inp(k, label):
        v = ins.get(k) or {}
        return [label, v.get("value"), ORIGIN.get(v.get("origin"), "Analyst" if v else ""),
                v.get("basis") or ("not entered" if not v else "")]

    _, _, r = _table(ws, r + 1, ["Input", "Value", "Origin", "Basis"],
                     [inp("eligible_income", "Eligible monthly income (₹)"),
                      inp("obligations", "Included monthly obligations (₹)"),
                      inp("foir_max_pct", "Illustrative max obligation-to-income (%)"),
                      inp("annual_rate_pct", "Annual interest rate (%)"),
                      inp("tenure_months", "Tenure (months)")],
                     money_cols=(1,), widths=[42, 16, 12, 80])
    _table(ws, r, ["Output (calculated)", "Value"],
           [["Status", elig["status"].replace("_", " ")],
            ["Existing obligation-to-income (%)", elig.get("existing_ratio_pct")],
            ["Max obligations at illustrative ratio (₹)", elig.get("max_obligation")],
            ["Illustrative additional EMI capacity (₹)", elig.get("capacity_emi")],
            ["Indicative principal (₹)", elig.get("principal")],
            ["Why provisional / invalid", " ".join(elig.get("provisional_reasons") or elig.get("errors") or [])],
            ["Missing inputs", ", ".join(elig.get("missing") or [])],
            ["Notes", " ".join(elig.get("notes") or [])],
            ["Formula", "capacity = max(0, ratio × income − obligations); principal = capacity × "
                        "(1 − (1+i)^−n) / i, i = annual rate / 12 (capacity × n when rate is 0)"]],
           money_cols=(1,))

    # --- Sources -----------------------------------------------------------------------------------
    ws = wb.create_sheet("Sources")
    r = _title(ws, "Source references for every extracted figure",
               "Status: Reported = verified against the page text or JSON; Unverified = proposed by a "
               "model, quote not found; Analyst = corrected or confirmed, with reason.")
    rows = []
    groups = [("Salary slip " + (s["month"] or ""), s["fields"]) for s in inc["slips"]]
    groups += [("Credit report", r_["fields"]) for r_ in out["credit"]["reports"]]
    groups += [(t["label"], t["fields"]) for t in out["credit"]["tradelines"]]
    groups += [("Statement " + (s["account"] or ""), s["fields"]) for s in bk["statements"]]
    groups += [("ITR", i["fields"]) for i in inc["itr"]]
    for name, fields in groups:
        for k, v in fields.items():
            corr = v["corrections"][-1] if v["corrections"] else None
            rows.append([name, k, _v(v), v["original"] if not v["reported_blank"] else "reported blank",
                         ORIGIN.get(v["status"], v["status"]), docs.get(v["document_id"], {}).get("filename"),
                         v["page_no"], v["json_path"], v["evidence_text"], v["note"],
                         corr["reason"] if corr else None])
    _table(ws, r, ["Item", "Field", "Value used", "Original extraction", "Status", "Document", "Page",
                   "JSON path", "Supporting text", "Note", "Correction reason"], rows,
           widths=[34, 20, 18, 18, 14, 32, 6, 30, 60, 40, 40])
    for w in wb.worksheets:
        w.freeze_panes = None
        w.sheet_view.showGridLines = False
    return wb


def _f(v):
    return None if v is None else float(v)
