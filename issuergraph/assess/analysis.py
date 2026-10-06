"""Everything derived for a case, computed from stored extractions + analyst input.

`build(case_id)` is the single source for the UI and the Excel export, so the
two cannot disagree. Nothing here is stored: a correction, a decision or an
assumption changes the next `build`, and every dependent figure follows.

Kinds of value, kept apart throughout:
  reported   — read from a document (verified anchor, or JSON path)
  inferred   — a match or classification proposed by this code
  analyst    — entered or confirmed by the analyst, with a reason/basis
  calculated — arithmetic over the above (eligibility.py)
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from ..db import connect
from . import banking as banking_mod, eligibility

GENERIC = {"BANK", "LTD", "LIMITED", "INDIA", "FINANCE", "FINANCIAL", "SERVICES", "CAPITAL", "PVT",
           "PRIVATE", "THE", "OF", "AND", "CO", "COMPANY", "TECHNOLOGIES", "SOLUTIONS", "NBFC",
           "HOUSING", "CREDIT", "ANALYTICS", "CARDS", "CARD"}
LOAN_WORDS = re.compile(r"\b(ACH|NACH|ECS|EMI|LOAN|SI|AUTOPAY|MANDATE|ACH D|ACH DR)\b", re.I)
SAL_WORDS = re.compile(r"\b(SAL|SALARY|SALARIES|PAYROLL)\b", re.I)
OD_INT = re.compile(r"\b(OD|OVERDRAFT)\b.*\bINT(EREST)?\b|\bINT(EREST)?\b.*\b(OD|OVERDRAFT)\b|"
                    r"\bOD INT\b|\bINT\.? ?DR\b|\bINTEREST DEBIT\b", re.I)
CARD_WORDS = re.compile(r"\b(CC|CREDIT CARD|CARD)\b", re.I)
D = Decimal


def _num(v):
    return None if v is None else D(str(v))


def _s(v):
    """JSON-friendly."""
    if isinstance(v, D):
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


ALIASES = {"AMEX": {"AMERICAN", "AMEX"}, "AMERICAN": {"AMERICAN", "AMEX"}, "SBI": {"SBI", "STATE"},
           "BOB": {"BOB", "BARODA"}}


def tokens(text: str | None) -> set[str]:
    out = {t for t in re.findall(r"[A-Z]{3,}", (text or "").upper()) if t not in GENERIC}
    return out | {a for t in out for a in ALIASES.get(t, ())}


def month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def month_label(key: str) -> str:
    y, m = map(int, key.split("-"))
    return date(y, m, 1).strftime("%b %Y")


# --- loading ---------------------------------------------------------------------

def _load(case_id: int) -> dict:
    with connect() as conn:
        q = lambda sql: conn.execute(sql, (case_id,)).fetchall()  # noqa: E731
        case = conn.execute("SELECT * FROM assess.case_file WHERE id=%s", (case_id,)).fetchone()
        if not case:
            return {}
        return {
            "case": case,
            "documents": q("SELECT d.*, r.pages AS run_pages, r.ocr_pages, r.model_id, r.model_calls, "
                           "r.input_tokens, r.output_tokens, r.retries, r.finished_at AS run_finished, "
                           "r.status AS run_status, r.version AS run_version, "
                           "(SELECT count(*) FROM assess.extraction_run x WHERE x.document_id=d.id) AS runs "
                           "FROM assess.document d LEFT JOIN LATERAL (SELECT * FROM assess.extraction_run "
                           "WHERE document_id=d.id ORDER BY id DESC LIMIT 1) r ON true "
                           "WHERE d.case_id=%s ORDER BY d.kind, d.doc_date NULLS LAST, d.id"),
            "items": q("SELECT * FROM assess.item WHERE case_id=%s ORDER BY document_id, seq"),
            "facts": q("SELECT f.*, i.seq AS item_seq FROM assess.fact f JOIN assess.item i ON i.id=f.item_id "
                       "WHERE f.case_id=%s ORDER BY f.id"),
            "txns": q("SELECT id, document_id, account_key, seq, txn_date, value_date, narration, ref, debit, "
                      "credit, balance, page_no FROM assess.txn WHERE case_id=%s "
                      "ORDER BY account_key, txn_date, document_id, seq"),
            "corrections": q("SELECT * FROM assess.correction WHERE case_id=%s ORDER BY id"),
            "assumptions": q("SELECT * FROM assess.assumption WHERE case_id=%s ORDER BY id"),
            "decisions": q("SELECT * FROM assess.decision WHERE case_id=%s ORDER BY id"),
            "acks": q("SELECT * FROM assess.review_ack WHERE case_id=%s ORDER BY id"),
        }


def fact_key(f) -> str:
    return f"{f['document_id']}:{f['item_seq']}:{f['field']}"


def txn_key(t) -> str:
    return f"{t['document_id']}:{t['seq']}"


def _latest(rows, key_fn):
    out = {}
    for r in rows:
        out[key_fn(r)] = r
    return out


# --- facts -------------------------------------------------------------------------

def _fact_view(f, corr_hist: list) -> dict:
    original = (f["value_num"] if f["value_num"] is not None else
                f["value_date"] if f["value_date"] is not None else f["value_text"])
    blank = f["value_text"] == "reported blank"
    if f["value_kind"] == "int" and f["value_num"] is not None:
        original = int(f["value_num"])
    view = {
        "key": fact_key(f), "id": f["id"], "field": f["field"], "kind": f["value_kind"],
        "document_id": f["document_id"], "page_no": f["page_no"], "origin": f["origin"],
        "verified": f["verified"], "evidence_text": f["evidence_text"], "json_path": f["json_path"],
        "note": f["note"], "has_rects": bool(f["rects"]),
        "original": None if blank else _s(original), "reported_blank": blank,
        "value": None if blank else _s(original), "status": "reported" if f["verified"] else "unverified",
        "corrections": [{"value": c["value"].get("value"), "reason": c["reason"],
                         "at": c["created_at"].isoformat()} for c in corr_hist],
    }
    if corr_hist:
        last = corr_hist[-1]["value"]
        view["value"] = last.get("value")
        view["status"] = "analyst"
        view["reported_blank"] = False if last.get("value") is not None else view["reported_blank"]
    return view


def _num_of(view: dict | None):
    """Usable number from a fact view: only reported (verified) or analyst values."""
    if not view or view["status"] == "unverified" or view["value"] is None:
        return None
    try:
        return D(str(view["value"]))
    except Exception:
        return None


# --- main ------------------------------------------------------------------------------

def build(case_id: int) -> dict:
    data = _load(case_id)
    if not data:
        return {}
    case = data["case"]
    corr = defaultdict(list)
    for c in data["corrections"]:
        corr[(c["target"], c["target_id"], c["field"])].append(c)

    docs = {d["id"]: d for d in data["documents"]}
    items = defaultdict(dict)                   # document_id -> seq -> {field: view}
    for f in data["facts"]:
        items[f["document_id"]].setdefault(f["item_seq"], {})[f["field"]] = \
            _fact_view(f, corr[("fact", fact_key(f), "value")])
    item_labels = {(i["document_id"], i["seq"]): i for i in data["items"]}

    out = {"case": {k: _s(v) for k, v in case.items()}, "generated_at": date.today().isoformat()}
    out["documents"] = [_doc_view(d) for d in data["documents"]]

    slips = _slips(docs, items)
    reports, tradelines = _credit(docs, items, item_labels)
    txns, statements, accounts = _banking(docs, items, data["txns"], corr, slips, tradelines)
    series = _series(txns)
    matches = _matches(tradelines, series, data["decisions"])
    obligations = _obligations(tradelines, series, matches, corr)
    income = _income(slips, txns, docs, items)
    od = _od(tradelines, series, matches, obligations, docs, items)
    assumptions = _assumptions(data["assumptions"])
    elig = _eligibility(assumptions, income, obligations)

    out.update(income=income, credit={"reports": reports, "tradelines": tradelines},
               banking={"statements": statements, "accounts": accounts,
                        "transactions": txns, "series": series,
                        "analysis": banking_mod.analyse(txns, statements)},
               business=_business(docs, items, statements),
               matches=matches, obligations=obligations, od=od, eligibility=elig)
    out["checklist"] = _checklist(case, out)
    out["review"] = _review(out, data["acks"])
    out["headline"] = _headline(out)
    out["eligibility"]["open_blocking"] = sum(1 for r in out["review"]
                                             if r["blocking"] and not r["resolved"])
    if out["eligibility"]["status"] == "computed" and out["eligibility"]["open_blocking"]:
        out["eligibility"]["status"] = "provisional"
    return out


def _doc_view(d) -> dict:
    v = {k: _s(d[k]) for k in ("id", "kind", "kind_basis", "filename", "status", "status_reasons", "source_label",
                                "doc_date", "period_start", "period_end", "page_count", "media_type",
                                "byte_size", "model_id", "model_calls", "input_tokens", "output_tokens",
                                "retries", "ocr_pages", "runs", "run_version")}
    v["uploaded_at"] = d["uploaded_at"].isoformat()
    v["processed_at"] = d["processed_at"].isoformat() if d["processed_at"] else None
    v["summary"] = d["summary"]
    return v


# --- income ------------------------------------------------------------------------

def _slips(docs, items) -> list[dict]:
    rows = []
    for doc_id, d in docs.items():
        if d["kind"] != "salary_slip":
            continue
        f = items.get(doc_id, {}).get(0, {})
        month = f.get("pay_month")
        gross, ded, net = (_num_of(f.get(k)) for k in ("gross_pay", "total_deductions", "net_pay"))
        check = None
        if gross is not None and ded is not None and net is not None:
            check = {"ok": gross - ded == net, "difference": _s(gross - ded - net)}
        rows.append({"document_id": doc_id, "status": d["status"], "fields": f,
                     "month": month["value"][:7] if month and month["value"] else None,
                     "employer": f.get("employer", {}).get("value") if f.get("employer") else None,
                     "gross": _s(gross), "deductions": _s(ded), "net": _s(net), "arith": check})
    rows.sort(key=lambda r: r["month"] or "")
    return rows


def _income(slips, txns, docs, items) -> dict:
    credits = [t for t in txns if t["category"] == "salary" and not t["duplicate"]]
    by_month = defaultdict(list)
    for t in credits:
        d = date.fromisoformat(t["txn_date"])
        # Salary is usually credited at the end of the month it pays for, or in
        # the first days of the next. Day ≥ 20 → that month; day ≤ 10 → previous.
        if d.day <= 10:
            d = d.replace(day=1) - timedelta(days=1)
        by_month[month_key(d)].append(t)

    covered = set()
    for doc in docs.values():
        if doc["kind"] == "bank_statement" and doc["period_start"] and doc["period_end"]:
            m = doc["period_start"].replace(day=1)
            while m <= doc["period_end"]:
                covered.add(month_key(m))
                m = (m + timedelta(days=32)).replace(day=1)

    months = sorted({s["month"] for s in slips if s["month"]} | set(by_month))
    comparison = []
    for m in months:
        slip = next((s for s in slips if s["month"] == m), None)
        cr = by_month.get(m, [])
        bank_total = sum((D(str(t["credit"])) for t in cr), D(0)) if cr else None
        net = D(str(slip["net"])) if slip and slip["net"] is not None else None
        diff = (bank_total - net) if bank_total is not None and net is not None else None
        if slip is None:
            note = "Salary credit seen in the bank statement; no salary slip for this month."
            level = "info"
        elif net is None:
            note = "Net pay on the slip is not verified."
            level = "review"
        elif not cr:
            nxt = (date.fromisoformat(m + "-01") + timedelta(days=40)).replace(day=1)
            if m not in covered and month_key(nxt) not in covered:
                note = "No statement covers the month this salary would be credited."
            else:
                note = "No salary credit identified in the statement for this month."
            level = "review"
        elif diff == 0:
            note = "Bank credit equals net pay."
            level = "ok"
        elif diff > 0:
            note = (f"Bank credit exceeds net pay by ₹{diff:,.2f}. Often a reimbursement, bonus or "
                    "arrears paid outside the slip — confirm with the employer's statement.")
            level = "review"
        else:
            note = (f"Bank credit is ₹{-diff:,.2f} below net pay. Possible split credit, advance "
                    "recovery or a credit to another account — confirm.")
            level = "review"
        comparison.append({"month": m, "label": month_label(m), "slip_net": slip["net"] if slip else None,
                           "slip_gross": slip["gross"] if slip else None,
                           "bank_credits": [{"id": t["id"], "date": t["txn_date"], "amount": t["credit"],
                                             "document_id": t["document_id"]} for t in cr],
                           "bank_total": _s(bank_total), "difference": _s(diff), "note": note,
                           "level": level})

    itr = []
    for doc_id, d in docs.items():
        if d["kind"] != "itr_json":
            continue
        f = items.get(doc_id, {}).get(0, {})
        itr.append({"document_id": doc_id, "status": d["status"], "summary": d["summary"], "fields": f})

    form16 = []
    for doc_id, d in docs.items():
        if d["kind"] != "form16_part_b":
            continue
        form16.append({"document_id": doc_id, "status": d["status"],
                       "fields": items.get(doc_id, {}).get(0, {})})
    form16.sort(key=lambda x: (x["fields"].get("assessment_year") or {}).get("value") or "")

    verified_slips = [s for s in slips if s["net"] is not None and s["gross"] is not None]
    recent = verified_slips[-3:]
    annual = None
    if verified_slips:
        avg_gross = sum((D(str(s["gross"])) for s in recent), D(0)) / len(recent)
        annual = {"value": _s((avg_gross * 12).quantize(D("1"))),
                  "basis": f"ASSUMPTION: average gross of {len(recent)} slip(s) "
                           f"({', '.join(month_label(s['month']) for s in recent)}) × 12. "
                           "Not a reported figure; ignores bonus, increments and variable pay."}
    suggestion = None
    if recent:
        avg_net = sum((D(str(s["net"])) for s in recent), D(0)) / len(recent)
        suggestion = {"value": _s(avg_net.quantize(D("0.01"))),
                      "basis": f"Suggested: average net pay of {len(recent)} verified slip(s) "
                               f"({', '.join(month_label(s['month']) for s in recent)})."}
    employers = sorted({s["employer"] for s in slips if s["employer"]})
    return {"slips": slips, "comparison": comparison, "itr": itr, "form16": form16, "annualised_gross": annual,
            "suggested_income": suggestion, "employers": employers,
            "salary_credit_rule": "Credits dated on/after the 20th count for that month; on/before the "
                                  "10th for the previous month."}


# --- credit report -------------------------------------------------------------------

def _credit(docs, items, labels):
    reports, tradelines = [], []
    for doc_id, d in docs.items():
        if d["kind"] != "credit_report":
            continue
        head = items.get(doc_id, {}).get(0, {})
        score = head.get("score")
        code = head.get("score_code")
        reports.append({"document_id": doc_id, "status": d["status"], "fields": head,
                        "supplied_by": "Applicant-supplied (not independently verified, not live)",
                        "score_state": ("reported" if score and score["value"] is not None else
                                        "no_score" if code else "not_found")})
        for seq, f in sorted(items.get(doc_id, {}).items()):
            if seq == 0:
                continue
            g = lambda k: f.get(k, {}).get("value") if f.get(k) else None  # noqa: E731
            acct_type = (g("account_type") or "").upper()
            own = (g("ownership") or "").upper()
            status = (g("status") or "").upper()
            closed = bool(g("date_closed")) or "CLOSED" in status or "SETTLED" in status \
                or "WRITTEN" in status and "OFF" in status
            kind = ("od" if re.search(r"OVERDRAFT|\bOD\b", acct_type) else
                    "card" if "CARD" in acct_type else "loan")
            acct = g("account_number")
            tradelines.append({
                "key": f"tl:{doc_id}:{seq}", "document_id": doc_id, "seq": seq,
                "label": labels.get((doc_id, seq), {}).get("label"), "fields": f,
                "lender": g("lender"), "account_type": g("account_type"), "ownership": g("ownership"),
                "ownership_role": ("guarantor" if "GUARANTOR" in own else "joint" if "JOINT" in own
                                   else "authorized user" if "AUTHORI" in own
                                   else "individual" if own else None),
                "status": g("status"), "closed": closed, "kind": kind,
                "last4": re.sub(r"\D", "", acct or "")[-4:] or None,
                "lender_tokens": sorted(tokens(g("lender"))),
            })
    return reports, tradelines


# --- banking -----------------------------------------------------------------------------

def _banking(docs, items, rows, corr, slips, tradelines):
    employer_tokens = set().union(*(tokens(s["employer"]) for s in slips if s["employer"])) \
        if slips else set()
    lender_tokens = set().union(*(set(t["lender_tokens"]) for t in tradelines)) if tradelines else set()
    # The statement's own bank appears in ordinary transfer narrations
    # ("NEFT ... ICICI ..."); it is not evidence of a loan from that bank.
    own_bank = {d["summary"].get("account_key"): tokens(d["summary"].get("bank"))
                for d in docs.values() if d["kind"] == "bank_statement" and d["summary"]}
    seen: dict[tuple, int] = {}
    txns = []
    for r in rows:
        nar = " ".join(r["narration"].split()).upper()
        dkey = (r["account_key"], r["txn_date"], r["debit"], r["credit"], r["balance"], nar)
        dup = None
        if not r["account_key"].endswith(f"doc{r['document_id']}") and dkey in seen \
                and seen[dkey] != r["document_id"]:
            dup = seen[dkey]
        else:
            seen.setdefault(dkey, r["document_id"])
        cat, basis = _classify(r, nar, employer_tokens, lender_tokens - own_bank.get(r["account_key"], set()))
        t = {"id": r["id"], "key": txn_key(r), "document_id": r["document_id"], "seq": r["seq"],
             "account_key": r["account_key"], "txn_date": r["txn_date"].isoformat(),
             "narration": r["narration"], "ref": r["ref"], "debit": _s(r["debit"]),
             "credit": _s(r["credit"]), "balance": _s(r["balance"]), "page_no": r["page_no"],
             "duplicate": dup is not None, "duplicate_of_document": dup,
             "auto_category": cat, "category": cat, "category_basis": basis, "category_origin": "inferred"}
        hist = corr.get(("txn", t["key"], "category"))
        if hist:
            t["category"] = hist[-1]["value"]["value"]
            t["category_origin"] = "analyst"
            t["category_basis"] = f"Analyst: {hist[-1]['reason']}"
        txns.append(t)

    statements, by_account = [], defaultdict(list)
    for doc_id, d in docs.items():
        if d["kind"] != "bank_statement":
            continue
        s = d["summary"] or {}
        f = items.get(doc_id, {}).get(0, {})
        statements.append({"document_id": doc_id, "status": d["status"], "filename": d["filename"],
                           "bank": s.get("bank"), "account": s.get("account"),
                           "account_key": s.get("account_key"),
                           "period_start": _s(d["period_start"]), "period_end": _s(d["period_end"]),
                           "summary": s, "fields": f,
                           "duplicates": sum(1 for t in txns if t["document_id"] == doc_id and t["duplicate"])})
        if s.get("account_key"):
            by_account[s["account_key"]].append(statements[-1])

    accounts = []
    for key, sts in by_account.items():
        sts.sort(key=lambda x: x["period_start"] or "")
        gaps, overlaps = [], []
        for a, b in zip(sts, sts[1:]):
            if not (a["period_end"] and b["period_start"]):
                continue
            ae, bs = date.fromisoformat(a["period_end"]), date.fromisoformat(b["period_start"])
            if bs > ae + timedelta(days=1):
                gaps.append({"from": (ae + timedelta(days=1)).isoformat(),
                             "to": (bs - timedelta(days=1)).isoformat()})
            elif bs <= ae:
                overlaps.append({"from": bs.isoformat(), "to": min(ae, date.fromisoformat(b["period_end"])).isoformat(),
                                 "documents": [a["document_id"], b["document_id"]]})
        own = [t for t in txns if t["account_key"] == key and not t["duplicate"]]
        starts = [s["period_start"] for s in sts if s["period_start"]]
        ends = [s["period_end"] for s in sts if s["period_end"]]
        accounts.append({"account_key": key, "bank": sts[0]["bank"], "account": sts[0]["account"],
                         "statements": [s["document_id"] for s in sts],
                         "from": min(starts) if starts else None, "to": max(ends) if ends else None,
                         "gaps": gaps, "overlaps": overlaps, "unique_rows": len(own),
                         "duplicate_rows": sum(1 for t in txns if t["account_key"] == key and t["duplicate"]),
                         "total_credits": _s(sum((D(str(t["credit"])) for t in own if t["credit"]), D(0))),
                         "total_debits": _s(sum((D(str(t["debit"])) for t in own if t["debit"]), D(0)))})
    return txns, statements, accounts


def _classify(r, nar, employer_tokens, lender_tokens):
    if r["credit"]:
        hit = employer_tokens & tokens(nar)
        if SAL_WORDS.search(nar) and hit:
            return "salary", f"Credit naming the employer ({', '.join(sorted(hit))}) and salary."
        if hit:
            return "salary", f"Credit naming the employer ({', '.join(sorted(hit))})."
        if SAL_WORDS.search(nar):
            return "salary", "Credit narration mentions salary; employer not named."
        return "other", None
    if OD_INT.search(nar):
        return "od_interest", "Debit narration reads as overdraft interest."
    if CARD_WORDS.search(nar) and re.search(r"AUTOPAY|PAYMENT|SI\b|BILL", nar):
        return "card_payment", "Debit narration reads as a credit-card payment."
    lt = lender_tokens & tokens(nar)
    if LOAN_WORDS.search(nar) or lt:
        why = []
        if LOAN_WORDS.search(nar):
            why.append("mandate/EMI wording")
        if lt:
            why.append("names a bureau lender (" + ", ".join(sorted(lt)) + ")")
        return "loan_repayment", "Debit with " + " and ".join(why) + "."
    return "other", None


def _signature(nar: str) -> str:
    words = [w for w in re.findall(r"[A-Z]{2,}", nar.upper()) if w not in {"UPI", "IMPS", "NEFT"}]
    return " ".join(words[:4])


def _series(txns):
    """Recurring debits of the servicing kinds, grouped by narration signature."""
    groups = defaultdict(list)
    for t in txns:
        if t["duplicate"] or t["category"] not in ("loan_repayment", "card_payment", "od_interest"):
            continue
        groups[(t["account_key"], t["category"], _signature(t["narration"]))].append(t)
    out = []
    for (acct, cat, sig), ts in groups.items():
        if cat != "od_interest" and len({t["txn_date"][:7] for t in ts}) < 2:
            continue        # a one-off debit is not a servicing pattern
        amounts = sorted(D(str(t["debit"])) for t in ts)
        months = sorted({t["txn_date"][:7] for t in ts})
        typical = amounts[len(amounts) // 2]
        fixed = amounts[-1] - amounts[0] <= max(D(1), typical * D("0.005"))
        out.append({"key": f"series:{acct}|{cat}|{sig}", "account_key": acct, "category": cat,
                    "signature": sig, "count": len(ts), "months": months,
                    "amount": _s(typical), "fixed_amount": fixed,
                    "min": _s(amounts[0]), "max": _s(amounts[-1]),
                    "average": _s((sum(amounts, D(0)) / len(amounts)).quantize(D("0.01"))),
                    "narrations": sorted({t["narration"] for t in ts})[:3],
                    "refs": sorted({t["ref"] for t in ts if t["ref"]})[:6],
                    "txn_ids": [t["id"] for t in ts], "recurring": len(months) >= 2})
    return sorted(out, key=lambda s: (-s["count"], s["signature"]))


# --- matching ------------------------------------------------------------------------------

COMPATIBLE = {"loan": ("loan_repayment",), "card": ("card_payment", "loan_repayment"),
              "od": ("od_interest", "loan_repayment")}


def _matches(tradelines, series, decisions):
    decided = _latest(decisions, lambda d: d["match_key"])
    cands = []
    for tl in tradelines:
        if tl["closed"]:
            continue
        emi = _num_of(tl["fields"].get("emi"))
        for s in series:
            if s["category"] not in COMPATIBLE[tl["kind"]]:
                continue
            text = " ".join(s["narrations"]).upper()
            signals = []
            if emi is not None and abs(D(str(s["amount"])) - emi) <= max(D(1), emi * D("0.005")):
                signals.append("amount")
            lt = set(tl["lender_tokens"]) & tokens(text)
            if lt:
                signals.append("lender")
            refs = text + " " + " ".join(str(x) for x in s.get("refs", []))
            if tl["last4"] and tl["last4"] in refs:
                signals.append("account")
            if s["category"] not in COMPATIBLE[tl["kind"]][:1] and "lender" not in signals \
                    and "account" not in signals:
                continue        # cross-category only with a lender or account signal
            acct_type = " ".join((tl["account_type"] or "").upper().split())
            if acct_type and acct_type in " ".join(text.split()):
                signals.append("type")
            if not signals or signals == ["amount"] and not s["recurring"]:
                continue
            if s["recurring"]:
                signals.append("monthly")
            cands.append({"key": f"{tl['key']}|{s['key']}", "tradeline": tl["key"], "series": s["key"],
                          "signals": signals,
                          "strength": "strong" if len({"amount", "lender", "account"} & set(signals)) >= 2
                          else "weak"})
    # A series claimed strongly by two tradelines is ambiguous for both.
    by_series = defaultdict(list)
    for c in cands:
        by_series[c["series"]].append(c)
    for c in cands:
        rivals = [o for o in by_series[c["series"]] if o is not c]
        if c["strength"] == "strong" and any(o["strength"] == "strong" for o in rivals):
            c["strength"] = "contested"
        d = decided.get(c["key"])
        if d:
            c["status"], c["reason"] = d["status"], d["reason"]
            c["decided_at"] = d["created_at"].isoformat()
        else:
            c["status"] = "proposed" if c["strength"] == "strong" else "ambiguous"
        c["explanation"] = _explain(c)
    return cands


def _explain(c):
    parts = {"amount": "amount equals the bureau EMI", "lender": "narration names the lender",
             "account": "narration/reference carries the account's last four digits",
             "monthly": "debited in more than one month",
             "type": "narration names the account type"}
    text = "; ".join(parts[s] for s in c["signals"])
    if c["strength"] == "contested":
        text += ". The same debits also match another account strongly — choose one."
    elif c["strength"] == "weak":
        text += ". One signal only — confirm or reject."
    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _active_match(matches, key, field):
    """The match a tradeline or series is resolved by: confirmed, else proposed."""
    live = [m for m in matches if m[field] == key and m["status"] in ("confirmed", "proposed")]
    live.sort(key=lambda m: m["status"] != "confirmed")
    return live[0] if live else None


# --- obligations ---------------------------------------------------------------------------

def _obligations(tradelines, series, matches, corr):
    rows = []
    series_by_key = {s["key"]: s for s in series}
    used_series = set()
    for tl in tradelines:
        f = tl["fields"]
        emi_view = f.get("emi")
        emi = _num_of(emi_view)
        m = _active_match(matches, tl["key"], "tradeline")
        if m:
            used_series.add(m["series"])
        observed = series_by_key.get(m["series"]) if m else None
        row = {"key": tl["key"], "source": "bureau", "label": tl["label"], "lender": tl["lender"],
               "account_type": tl["account_type"], "kind": tl["kind"], "ownership": tl["ownership_role"],
               "document_id": tl["document_id"],
               "outstanding": _s(_num_of(f.get("current_balance"))),
               "sanctioned": _s(_num_of(f.get("sanctioned_amount"))),
               "limit": _s(_num_of(f.get("credit_limit"))),
               "bureau_emi": _s(emi), "bureau_emi_state": _state(emi_view),
               "facts": {k: f[k]["id"] for k in ("current_balance", "emi", "sanctioned_amount",
                                                    "credit_limit", "lender") if f.get(k)},
               "observed": ({"amount": observed["amount"], "months": observed["months"],
                             "txn_ids": observed["txn_ids"],
                             "fixed": observed["fixed_amount"], "match": m["key"], "match_status": m["status"]}
                            if observed else None),
               "included": False, "amount": None, "origin": None, "basis": None, "unresolved": None}
        if tl["closed"]:
            row.update(basis="Closed per the credit report; not an obligation.", origin="reported")
        elif tl["kind"] == "od":
            row["unresolved"] = ("Overdraft: no fixed EMI. Enter an assumed monthly obligation with "
                                 "its basis, or exclude it with a reason.")
        elif tl["kind"] == "card":
            row["unresolved"] = ("Credit card: no fixed EMI reported. Enter an assumed obligation "
                                 "with its basis, or exclude it with a reason.")
        elif tl["ownership_role"] in ("guarantor", "authorized user"):
            row["unresolved"] = (f"Applicant is {tl['ownership_role']}. Decide whether this obligation "
                                 "counts, with a reason.")
        elif emi is not None and emi > 0:
            row.update(included=True, amount=_s(emi), origin=emi_view["status"],
                       basis="EMI as reported in the credit report" if emi_view["status"] == "reported"
                       else "EMI as corrected by analyst")
        elif observed and m["status"] in ("confirmed", "proposed") and observed["fixed_amount"]:
            row.update(included=True, amount=observed["amount"], origin="inferred",
                       basis=f"Bureau EMI {row['bureau_emi_state']}; amount taken from the matched "
                             f"monthly bank debit ({m['status']} match).")
        elif emi_view and emi_view["status"] == "unverified":
            row["unresolved"] = "Bureau EMI could not be verified against the report."
        else:
            row["unresolved"] = (f"Bureau EMI {row['bureau_emi_state']} and no matching bank debit. "
                                 "Enter the obligation with its basis, or exclude it.")
        _apply_treatment(row, corr)
        rows.append(row)

    for s in series:
        if s["key"] in used_series or s["category"] != "loan_repayment" or not s["recurring"]:
            continue
        rejected_all = all(m["status"] == "rejected" for m in matches if m["series"] == s["key"]) \
            if any(m["series"] == s["key"] for m in matches) else True
        if not rejected_all:
            continue
        row = {"key": s["key"], "source": "bank", "label": f"Recurring debit: {s['signature']}",
               "lender": None, "account_type": None, "kind": "bank_only", "ownership": None,
               "document_id": None, "outstanding": None, "sanctioned": None, "limit": None, "facts": {},
               "txn_ids": s["txn_ids"],
               "bureau_emi": None, "bureau_emi_state": "not in credit report",
               "observed": {"amount": s["amount"], "months": s["months"], "fixed": s["fixed_amount"]},
               "included": False, "amount": None, "origin": None, "basis": None,
               "unresolved": ("Recurring loan-like debit with no account in the credit report. "
                              "Include it as an obligation or exclude it, with a reason.")}
        _apply_treatment(row, corr)
        rows.append(row)
    total = sum((D(str(r["amount"])) for r in rows if r["included"] and r["amount"] is not None), D(0))
    return {"rows": rows, "total_included": _s(total),
            "unresolved": sum(1 for r in rows if r["unresolved"])}


def _state(view):
    if view is None:
        return "not reported"
    if view["reported_blank"]:
        return "reported blank"
    if view["status"] == "unverified":
        return "unverified"
    return "reported"


def _apply_treatment(row, corr):
    hist = corr.get(("obligation", row["key"], "treatment"))
    if not hist:
        return
    v = hist[-1]["value"]
    row["included"] = bool(v.get("include"))
    row["amount"] = v.get("amount") if row["included"] else None
    row["origin"] = "analyst"
    row["basis"] = f"Analyst: {v.get('basis') or hist[-1]['reason']}"
    row["unresolved"] = None
    row["analyst_reason"] = hist[-1]["reason"]


# --- OD ------------------------------------------------------------------------------------

def _od(tradelines, series, matches, obligations, docs, items):
    out = []
    for tl in tradelines:
        if tl["kind"] != "od":
            continue
        f = tl["fields"]
        m = _active_match(matches, tl["key"], "tradeline")
        s = next((x for x in series if m and x["key"] == m["series"]), None)
        sanction = []
        for doc_id, d in docs.items():
            if d["kind"] == "od_sanction":
                sanction.append({"document_id": doc_id, "fields": items.get(doc_id, {}).get(0, {})})
        ob = next(r for r in obligations["rows"] if r["key"] == tl["key"])
        out.append({"key": tl["key"], "lender": tl["lender"], "label": tl["label"], "fields": f,
                    "sanctioned_limit": _s(_num_of(f.get("sanctioned_amount")) or _num_of(f.get("credit_limit"))),
                    "outstanding": _s(_num_of(f.get("current_balance"))),
                    "drawing_power": _s(_num_of(f.get("drawing_power"))),
                    "drawing_power_state": _state(f.get("drawing_power")),
                    "interest_series": s, "interest_match": m["status"] if m else None,
                    "sanction_documents": sanction, "obligation": ob,
                    "rule": "The sanctioned limit is not debt and not an EMI. Only the outstanding "
                            "balance is owed; any monthly obligation must be an analyst assumption."})
    return out


# --- assumptions & eligibility ---------------------------------------------------------------

ASSUMPTION_KEYS = ("eligible_income", "foir_max_pct", "annual_rate_pct", "tenure_months")


def _assumptions(rows):
    latest = _latest(rows, lambda r: r["key"])
    hist = defaultdict(list)
    for r in rows:
        hist[r["key"]].append({"value": _s(r["value"]), "basis": r["basis"], "at": r["created_at"].isoformat()})
    return {k: ({"value": _s(latest[k]["value"]), "basis": latest[k]["basis"],
                 "at": latest[k]["created_at"].isoformat(), "history": hist[k]} if k in latest else None)
            for k in ASSUMPTION_KEYS}


def _eligibility(a, income, obligations):
    inc = a["eligible_income"]
    income_value = inc["value"] if inc else None
    income_basis = inc["basis"] if inc else None
    income_origin = "analyst" if inc else None
    if not inc and income["suggested_income"]:
        income_value = income["suggested_income"]["value"]
        income_basis = income["suggested_income"]["basis"] + " Not yet confirmed by the analyst."
        income_origin = "suggested"
    result = eligibility.compute(
        income=income_value,
        obligations=obligations["total_included"],
        foir_max_pct=a["foir_max_pct"]["value"] if a["foir_max_pct"] else None,
        annual_rate_pct=a["annual_rate_pct"]["value"] if a["annual_rate_pct"] else None,
        tenure_months=a["tenure_months"]["value"] if a["tenure_months"] else None)
    result["inputs"] = {
        "eligible_income": {"value": income_value, "basis": income_basis, "origin": income_origin},
        "obligations": {"value": obligations["total_included"], "origin": "calculated",
                        "basis": "Sum of included rows in the liability schedule.",
                        "unresolved_rows": obligations["unresolved"]},
        "foir_max_pct": a["foir_max_pct"], "annual_rate_pct": a["annual_rate_pct"],
        "tenure_months": a["tenure_months"]}
    reasons = list(result.get("errors", []))
    if result["status"] == "computed":
        if income_origin == "suggested":
            reasons.append("Eligible income is a suggestion, not an analyst-confirmed figure.")
        if obligations["unresolved"]:
            reasons.append(f"{obligations['unresolved']} liability row(s) unresolved — their "
                           "obligations are not in the total.")
        if reasons:
            result["status"] = "provisional"
    result["provisional_reasons"] = reasons
    result["label"] = "Illustrative policy assumptions — not a lender approval or sanction."
    return result


# --- checklist & review ------------------------------------------------------------------------

VINTAGE_GUIDE_YEARS = 3     # from the advisor's own checklist; lender criteria vary


def _business(docs, items, statements) -> dict:
    regs = []
    for doc_id, d in docs.items():
        if d["kind"] not in ("gst_certificate", "udyam_certificate"):
            continue
        f = items.get(doc_id, {}).get(0, {})
        regs.append({"document_id": doc_id, "kind": d["kind"], "status": d["status"], "fields": f})
    dated = []
    for r in regs:
        for k in ("liability_date", "validity_from", "incorporation_date", "commencement_date"):
            v = r["fields"].get(k)
            if v and v["status"] != "unverified" and v["value"]:
                dated.append((v["value"], k, v))
    earliest = min(dated, key=lambda x: x[0]) if dated else None
    years = None
    if earliest:
        d0 = date.fromisoformat(str(earliest[0])[:10])
        years = round((date.today() - d0).days / 365.25, 1)
    types = sorted({(s["summary"] or {}).get("account_type") or "type not stated" for s in statements})
    return {"registrations": regs,
            "vintage": {"years": years, "since": earliest[0] if earliest else None,
                        "basis": (f"Earliest of the registration dates read ({earliest[1].replace('_', ' ')})"
                                  if earliest else None), "fact": earliest[2] if earliest else None,
                        "guide_years": VINTAGE_GUIDE_YEARS},
            "account_types": types, "has_current_account": "current" in types}


def _checklist(case, out):
    if case["borrower_type"] == "proprietorship":
        return _checklist_proprietorship(out)
    return _checklist_salaried(case, out)


def _months_covered(statements, account_type):
    sts = [s for s in statements if (s["summary"] or {}).get("account_type") == account_type
           and s["period_start"] and s["period_end"]]
    if not sts:
        return None, None
    return min(s["period_start"] for s in sts), max(s["period_end"] for s in sts)


def _checklist_proprietorship(out):
    docs = out["documents"]
    kinds = lambda *k: [d for d in docs if d["kind"] in k]  # noqa: E731
    rows = []

    def row(item, got, missing, ok):
        rows.append({"item": item, "received": got or None, "missing": None if ok else missing, "ok": ok})

    kyc = kinds("kyc")
    kyc_names = ", ".join(sorted({(d["kind_basis"] or "").split("(")[-1].rstrip(").") for d in kyc}))
    row("Applicant KYC — photo, PAN, Aadhaar", kyc_names, "Not received", bool(kyc))
    gst, alt = kinds("gst_certificate"), kinds("business_proof")
    row("GST certificate (or alternative business proof)",
        ", ".join(d["filename"] for d in gst + alt), "Not received", bool(gst or alt))
    row("MSME / Udyam certificate", ", ".join(d["filename"] for d in kinds("udyam_certificate")),
        "Not received", bool(kinds("udyam_certificate")))
    itr = kinds("itr_ack", "itr_json", "itr_computation")
    row("ITR — last 2 years, with computation", f"{len(itr)} document(s)" if itr else None,
        "Not received" if not itr else "Fewer than 2 years / computation not identified",
        len(itr) >= 2 and bool(kinds("itr_computation", "itr_json")))
    row("P&L and balance sheet (where applicable)", ", ".join(d["filename"] for d in kinds("financials")),
        "Not received — confirm whether applicable", bool(kinds("financials")))
    row("Business / ownership proof", ", ".join(d["filename"] for d in alt), "Not received", bool(alt))
    for acct_type, months, label in (("current", 12, "Current account statement — last 12 months"),
                                     ("savings", 6, "Savings account statement — last 6 months")):
        frm, to = _months_covered(out["banking"]["statements"], acct_type)
        if not frm:
            row(label, None, "Not received", False)
            continue
        f, t = date.fromisoformat(frm), date.fromisoformat(to)
        idx = t.year * 12 + t.month - 1 - (months - 1)
        want = date(idx // 12, idx % 12 + 1, 1)
        short = f > want + timedelta(days=3)
        row(label, f"{f:%d %b %Y} – {t:%d %b %Y}", f"Covers from {f:%d %b %Y}; {months} months would start "
            f"{want:%d %b %Y}", not short)
    open_loans = [t for t in out["credit"]["tradelines"] if not t["closed"] and t["kind"] in ("loan", "od")]
    open_loans += [s_ for s_ in out["banking"]["series"] if s_["category"] == "loan_repayment" and s_["recurring"]]
    soa = kinds("loan_soa", "od_sanction", "loan_sanction")
    row("Existing loan SOA / sanction letter (if applicable)", ", ".join(d["filename"] for d in soa),
        f"{len(open_loans)} open loan(s) seen in the credit report or as recurring EMIs; no SOA received" if open_loans
        else "No open loans seen", bool(soa) or not open_loans)
    return {"rows": rows, "note": "Proprietorship checklist from the advisor's process note — adjust to "
                                  "each lender's own list."}


def _checklist_salaried(case, out):
    docs = out["documents"]
    rows = []
    slips = sorted(s["month"] for s in out["income"]["slips"] if s["month"])
    expected_slips = []
    if slips:
        last = date.fromisoformat(slips[-1] + "-01")
        m = last
        for _ in range(3):
            expected_slips.append(month_key(m))
            m = (m - timedelta(days=1)).replace(day=1)
    missing_slips = [month_label(m) for m in sorted(expected_slips) if m not in slips]
    rows.append({"item": "Salary slips — latest 3 months",
                 "received": ", ".join(month_label(m) for m in slips) or None,
                 "missing": (", ".join(missing_slips) or None) if slips else "No salary slip received",
                 "ok": bool(slips) and not missing_slips})
    accts = out["banking"]["accounts"]
    if accts:
        a = max(accts, key=lambda x: x["unique_rows"])
        to = date.fromisoformat(a["to"]) if a["to"] else None
        frm = date.fromisoformat(a["from"]) if a["from"] else None
        want_from = (to.replace(day=1) - timedelta(days=150)).replace(day=1) if to else None
        miss = []
        if frm and want_from and frm > want_from:
            miss.append(f"{want_from:%d %b %Y} – {frm - timedelta(days=1):%d %b %Y}")
        miss += [f"{date.fromisoformat(g['from']):%d %b %Y} – {date.fromisoformat(g['to']):%d %b %Y}"
                 for g in a["gaps"]]
        rows.append({"item": "Bank statement (salary account) — 6 months",
                     "received": f"{a['bank'] or ''} {a['account'] or ''}: {frm:%d %b %Y} – {to:%d %b %Y}"
                     if frm and to else "Received; period not read",
                     "missing": "; ".join(miss) or None, "ok": not miss})
    else:
        rows.append({"item": "Bank statement (salary account) — 6 months", "received": None,
                     "missing": "No bank statement processed", "ok": False})
    cr = [d for d in docs if d["kind"] == "credit_report"]
    rows.append({"item": "Credit report (applicant-supplied)",
                 "received": ", ".join(f"{d['source_label'] or 'report'} dated {d['doc_date'] or 'unknown'}"
                                       for d in cr) or None,
                 "missing": None if cr else "Not received", "ok": bool(cr)})
    itr = out["income"]["itr"]
    f16 = out["income"]["form16"]
    got = [f"{(i['summary'] or {}).get('assessment_year', 'AY ?')} ITR — {(i['summary'] or {}).get('filed_status', '')}"
           for i in itr] + [f"Form 16 {(f['fields'].get('assessment_year') or {}).get('value', '')}" for f in f16]
    rows.append({"item": "ITR or Form 16 (latest year)", "received": ", ".join(got) or None,
                 "missing": None if got else "Not received", "ok": bool(got)})
    if out["od"]:
        has = any(o["sanction_documents"] for o in out["od"])
        rows.append({"item": "OD sanction letter / OD account statement",
                     "received": "Sanction letter" if has else None,
                     "missing": None if has else "Not received — drawing power and terms unknown",
                     "ok": has})
    return {"rows": rows, "note": "Demo checklist for a salaried case — adjust to each lender's own list."}


MISSING, DISCREPANCY, CONFIRM = "missing", "discrepancy", "confirm"
QUICK_DAYS_TEXT = "2 days"
GROUP_LABEL = {MISSING: "Missing information", DISCREPANCY: "Possible discrepancy",
               CONFIRM: "Needs confirmation"}


def _review(out, acks):
    acked = _latest(acks, lambda a: a["item_key"])
    items = []

    def add(key, group, area, text, blocking, action="ack", target=None, tab=None):
        a = acked.get(key)
        resolved = bool(a) and action == "ack"
        items.append({"key": key, "group": group, "group_label": GROUP_LABEL[group], "area": area,
                      "text": text, "blocking": blocking, "action": action, "target": target,
                      "tab": tab, "resolved": resolved, "resolution": a["note"] if resolved else None})

    for d in out["documents"]:
        reasons = " ".join(d["status_reasons"] or [])
        if d["status"] in ("failed", "unsupported", "processing", "received") and d["kind"] != "kyc":
            add(f"doc:{d['id']}:{d['status']}", MISSING, "Documents",
                f"{d['filename']}: not read ({d['status']}). {reasons}", True, tab="documents")
        elif d["status"] == "incomplete":
            unverified = "could not be verified" in reasons
            add(f"doc:{d['id']}:incomplete", CONFIRM if unverified else MISSING, "Documents",
                f"{d['filename']}: incomplete. {reasons}",
                d["kind"] in ("credit_report", "bank_statement", "salary_slip"), tab="documents")
    for r in out["checklist"]["rows"]:
        if r["missing"]:
            add(f"checklist:{r['item']}", MISSING, "Documents", f"{r['item']}: {r['missing']}.", False,
                tab="documents")
    for tl in out["credit"]["tradelines"]:
        for field, v in tl["fields"].items():
            if v["status"] == "unverified":
                add(f"fact:{v['key']}", CONFIRM, "Credit report",
                    f"{tl['label']}: {field.replace('_', ' ')} proposed by the model but not verified "
                    f"({v['note']}).", field in ("emi", "current_balance", "overdue"), "fact", v["key"],
                    tab="liabilities")
        overdue = tl["fields"].get("overdue")
        if overdue and overdue["status"] != "unverified" and overdue["value"] and float(overdue["value"]) > 0:
            add(f"overdue:{tl['key']}", DISCREPANCY, "Credit report",
                f"{tl['label']}: overdue ₹{float(overdue['value']):,.0f} reported.", False,
                tab="liabilities")
    for m in out["matches"]:
        tl = next(t for t in out["credit"]["tradelines"] if t["key"] == m["tradeline"])
        if m["status"] == "ambiguous":
            add(f"match:{m['key']}", CONFIRM, "Liabilities",
                f"Possible match: {tl['label']} ↔ bank debits “{m['series'].split('|')[-1]}”. "
                f"{m['explanation']}", True, "match", m["key"], tab="liabilities")
        elif m["status"] == "proposed":
            add(f"match:{m['key']}", CONFIRM, "Liabilities",
                f"Inferred match: {tl['label']} ↔ bank debits “{m['series'].split('|')[-1]}”. "
                f"{m['explanation']}", False, "match", m["key"], tab="liabilities")
    for r in out["obligations"]["rows"]:
        if r["unresolved"]:
            no_emi = r["kind"] in ("od", "card") or "not reported" in r["unresolved"] or \
                "blank" in r["unresolved"]
            group = MISSING if no_emi and r["source"] == "bureau" and r["ownership"] not in \
                ("guarantor", "authorized user") else CONFIRM
            add(f"obligation:{r['key']}", group, "Liabilities", f"{r['label']}: {r['unresolved']}", True,
                "obligation", r["key"], tab="liabilities")
    for s in out["banking"]["statements"]:
        sm = s["summary"] or {}
        if sm.get("recon_ok") is False or sm.get("running_breaks"):
            add(f"recon:{s['document_id']}", DISCREPANCY, "Banking",
                f"{s['filename']}: balances do not reconcile — " + " ".join(sm.get("issues", [])), True,
                tab="documents")
    for a in out["banking"]["accounts"]:
        for g in a["gaps"]:
            add(f"gap:{a['account_key']}:{g['from']}", MISSING, "Banking",
                f"{a['bank']} {a['account']}: no statement for {g['from']} to {g['to']}.", False,
                tab="documents")
    for c in out["income"]["comparison"]:
        if c["level"] == "review":
            group = DISCREPANCY if c["difference"] not in (None, 0) else \
                CONFIRM if "not verified" in c["note"] else MISSING
            add(f"income:{c['month']}", group, "Income", f"{c['label']}: {c['note']}", False, tab="income")
    for s in out["income"]["slips"]:
        if s["arith"] and not s["arith"]["ok"]:
            add(f"slip-arith:{s['document_id']}", DISCREPANCY, "Income",
                f"Slip {s['month']}: gross − deductions ≠ net (difference {s['arith']['difference']}).",
                False, tab="income")
    for i in out["income"]["itr"]:
        st = (i["summary"] or {}).get("filed_status", "")
        if "not" in st:
            add(f"itr:{i['document_id']}", MISSING, "Income", f"ITR JSON: {st}.", False, tab="income")
    if out["case"]["borrower_type"] == "proprietorship":
        b = out["business"]
        v = b["vintage"]
        if v["years"] is None:
            add("biz:vintage", MISSING, "Business", "Business vintage cannot be established: no GST or "
                "Udyam registration date has been read.", False, tab="banking")
        elif v["years"] < v["guide_years"]:
            add("biz:vintage", CONFIRM, "Business", f"Business vintage {v['years']} years from "
                f"{v['since']} — below the {v['guide_years']}-year guide in the checklist (lender "
                "criteria vary).", False, tab="banking")
        if not b["has_current_account"]:
            add("biz:current", MISSING, "Business", "No current-account statement identified; business "
                "credits may be running through a savings account.", False, tab="documents")
    for a in out["banking"]["analysis"]:
        name = a["account_key"].replace("|", " ")
        th = a["review_thresholds"]
        if a["returns"]["count"]:
            add(f"bank:returns:{a['account_key']}", DISCREPANCY, "Banking",
                f"{name}: {a['returns']['count']} cheque/ECS/NACH return or bounce entr(y/ies).", False,
                tab="banking")
        if a["negative_days"] and (a["account_type"] in ("savings", "current", None)):
            add(f"bank:neg:{a['account_key']}", DISCREPANCY, "Banking",
                f"{name}: balance below zero on {a['negative_days']} day(s).", False, tab="banking")
        cs = a["cash_deposits"]["share"]
        if cs is not None and cs >= th["cash_share"]:
            add(f"bank:cash:{a['account_key']}", CONFIRM, "Banking",
                f"{name}: cash deposits are {cs * 100:.0f}% of business credits — confirm the business "
                "reason (review prompt, not a lender rule).", False, tab="banking")
        if out["case"]["borrower_type"] == "salaried":
            continue        # salary from one employer is expected; not a dependence finding
        if a["account_type"] == "savings":
            continue        # dependence and retention are read on the business account
        top = a["concentration"]["top"][:1]
        if top and top[0]["share"] is not None and top[0]["share"] >= th["concentration_share"]:
            add(f"bank:conc:{a['account_key']}", CONFIRM, "Banking",
                f"{name}: {top[0]['share'] * 100:.0f}% of non-cash business credits come from one party "
                f"({top[0]['party']}) — check dependence on a single customer.", False, tab="banking")
        q = a["retention"]["share"]
        if q is not None and q >= th["quick_out_share"]:
            add(f"bank:quick:{a['account_key']}", CONFIRM, "Banking",
                f"{name}: {q * 100:.0f}% of large credits (by value) left the account within "
                f"{QUICK_DAYS_TEXT}.", False, tab="banking")
    e = out["eligibility"]
    if e["inputs"]["eligible_income"]["origin"] == "suggested":
        add("elig:income", CONFIRM, "Eligibility", "Eligible monthly income is a suggestion; enter the "
            "figure and its basis.", True, "assumption", "eligible_income", tab="eligibility")
    seen, uniq = set(), []
    for it in items:
        if it["key"] not in seen:
            seen.add(it["key"])
            uniq.append(it)
    return uniq


def _headline(out) -> dict:
    """Overview figures, each with its date and source; None means Not available."""
    slips = [x for x in out["income"]["slips"] if x["net"] is not None]
    latest = slips[-1] if slips else None
    f16 = [f for f in out["income"]["form16"] if (f["fields"].get("gross_salary") or {}).get("value") is not None]
    f16_latest = f16[-1] if f16 else None
    reports = out["credit"]["reports"]
    open_all = [t for t in out["credit"]["tradelines"] if not t["closed"]]
    open_tl = [t for t in open_all if t["ownership_role"] not in ("guarantor", "authorized user")]
    excluded_roles = [f"{t['label']} ({t['ownership_role']})" for t in open_all if t not in open_tl]
    bal = [(t, t["fields"].get("current_balance")) for t in open_tl]
    known = [(t, v) for t, v in bal if v and v["status"] != "unverified" and v["value"] is not None]
    unknown = [t["label"] for t, v in bal if not (v and v["status"] != "unverified" and v["value"] is not None)]
    rdate = (reports[0]["fields"].get("report_date") or {}) if reports else {}
    groups = {g: sum(1 for r in out["review"] if r["group"] == g and not r["resolved"])
              for g in GROUP_LABEL}
    return {
        "income": {
            "net_pay": latest["fields"]["net_pay"] if latest else None,
            "net_pay_month": latest["month"] if latest else None,
            "form16_gross": f16_latest["fields"]["gross_salary"] if f16_latest else None,
            "form16_ay": (f16_latest["fields"].get("assessment_year") or {}).get("value") if f16_latest else None,
            "eligible": out["eligibility"]["inputs"]["eligible_income"],
        },
        "debt": {
            "outstanding": _s(sum((D(str(v["value"])) for _, v in known), D(0))) if known else None,
            "accounts": len(open_tl), "counted": len(known), "not_counted": unknown,
            "excluded_roles": excluded_roles,
            "parts": [{"label": t["label"], "kind": t["kind"], "fact": v} for t, v in known],
            "report_date": rdate.get("value"), "report_fact": rdate.get("id"),
            "report_document": reports[0]["document_id"] if reports else None,
        },
        "obligations": {"total": out["obligations"]["total_included"],
                        "unresolved": out["obligations"]["unresolved"],
                        "rows_included": sum(1 for r in out["obligations"]["rows"] if r["included"])},
        "findings": groups,
        "coverage": {"ok": sum(1 for r in out["checklist"]["rows"] if r["ok"]),
                     "of": len(out["checklist"]["rows"]),
                     "documents": len(out["documents"]),
                     "read": sum(1 for d in out["documents"] if d["status"] in ("complete", "identified")),
                     "not_read": sum(1 for d in out["documents"] if d["status"] in ("failed", "unsupported")),
                     "incomplete": sum(1 for d in out["documents"] if d["status"] == "incomplete")},
    }
