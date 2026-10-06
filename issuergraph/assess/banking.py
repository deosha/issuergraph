"""Banking analysis over parsed, de-duplicated statement rows.

Deterministic arithmetic; every figure carries the transaction ids behind it,
so the UI can open the rows on the statement page. Thresholds that decide
whether something is *shown as a finding* are review prompts stated here, not
lender policy, and the UI says so.

  AMB      average of end-of-day balances over the days each statement covers
           (the balance carries forward on days without a transaction).
  Credits  all credits, and "business credits" = credits less identified
           reversals/returns and own-account transfers.
  Cash     deposits and withdrawals identified from narration wording.
  Returns  cheque/ECS/NACH returns and their charges, from narration wording.
  Retention  large credits matched by subsequent debits (≥ 80% of their value within
             2 days), each debited rupee counted once — a timing pattern, not money traced.
  Concentration  business credits grouped by the counterparty name read from
           the narration (inferred).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

D = Decimal

CASH_DEP = re.compile(r"\b(CASH DEP|CASH DEPOSIT|BY CASH|CDM|CASH-DEP|CSH DEP)\b", re.I)
CASH_WDL = re.compile(r"\b(ATM|ATW|NWD|CASH WDL|CASH WITHDRAWAL|CASH-WDL|SELF CHQ|CSH WDL)\b", re.I)
RETURN = re.compile(r"\b(RETURN|RTN|RET|BOUNCE|BOUNCED|DISHONOU?R|INSUFFICIENT|UNPAID|REJECT(ED)?)\b", re.I)
CHARGE = re.compile(r"\b(CHG|CHGS|CHARGES?|PENALTY|FEE)\b", re.I)
SELF = re.compile(r"\b(SELF|OWN A/?C|TRF TO SB|TRF FROM SB|SWEEP)\b", re.I)
CHANNEL = {"UPI", "NEFT", "RTGS", "IMPS", "CR", "DR", "INFT", "BIL", "MMT", "ACH", "NACH", "ECS", "TRF", "TO",
           "FROM", "BY", "PAYMENT", "PAY", "TRANSFER", "FT", "IB", "MB", "NETBANKING", "REF", "CMS", "INB"}

LARGE_CREDIT = D(10000)        # credits considered for retention
QUICK_DAYS = 2
QUICK_SHARE = D("0.8")
REVIEW = {"concentration_share": 0.5, "cash_share": 0.3, "quick_out_share": 0.5}


def counterparty(narration: str) -> str | None:
    parts = re.split(r"[-/:]", narration.upper())
    words = []
    for p in parts:
        p = p.strip()
        if not p or p in CHANNEL or re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", p) or \
                sum(ch.isdigit() for ch in p) > len(p) / 3 or "@" in p:
            continue
        toks = [t for t in re.findall(r"[A-Z]{2,}", p) if t not in CHANNEL]
        if toks:
            words = toks[:3]
            break
    return " ".join(words) or None


def _month(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def analyse(transactions: list[dict], statements: list[dict]) -> list[dict]:
    by_acct = defaultdict(list)
    for t in transactions:
        if not t["duplicate"]:
            by_acct[t["account_key"]].append(t)
    out = []
    for key, rows in by_acct.items():
        sts = sorted([s for s in statements if s.get("account_key") == key and s["period_start"]],
                     key=lambda s: s["period_start"])
        rows.sort(key=lambda t: (t["txn_date"], t["document_id"], t["seq"]))
        out.append(_account(key, rows, sts))
    order = {"current": 0, "cash credit": 1, "overdraft": 1, None: 2, "savings": 3}
    return sorted(out, key=lambda a: order.get(a["account_type"], 2))


def _account(key, rows, sts) -> dict:
    acct_type = next((s["summary"].get("account_type") for s in sts if s["summary"].get("account_type")), None)
    cover = [(date.fromisoformat(s["period_start"]), date.fromisoformat(s["period_end"])) for s in sts
             if s["period_start"] and s["period_end"]]
    start = min(c[0] for c in cover) if cover else date.fromisoformat(rows[0]["txn_date"])
    end = max(c[1] for c in cover) if cover else date.fromisoformat(rows[-1]["txn_date"])
    opening = None
    if sts and sts[0]["summary"].get("opening_balance") is not None:
        opening = D(str(sts[0]["summary"]["opening_balance"]))

    # end-of-day balances
    eod: dict[date, D] = {}
    for t in rows:
        if t["balance"] is not None:
            eod[date.fromisoformat(t["txn_date"])] = D(str(t["balance"]))
    has_bal = bool(eod) and opening is not None
    starts = {}
    for st in sts:
        if st["period_start"] and st["summary"].get("opening_balance") is not None:
            starts.setdefault(date.fromisoformat(st["period_start"]), D(str(st["summary"]["opening_balance"])))
    daily = {}
    bal = opening
    d = start
    covered = lambda day: any(a <= day <= b for a, b in cover) or not cover  # noqa: E731
    while d <= end and has_bal:
        if covered(d) and not covered(d - timedelta(days=1)) and d in starts:
            bal = starts[d]          # a new statement after a gap opens with its own balance
        if d in eod:
            bal = eod[d]
        if covered(d):
            daily[d] = bal
        d += timedelta(days=1)

    months = defaultdict(lambda: {"credits": D(0), "credit_n": 0, "debits": D(0), "debit_n": 0,
                                  "business_credits": D(0), "cash_dep": D(0), "cash_wdl": D(0),
                                  "returns": 0, "days": 0, "bal_sum": D(0), "min_bal": None,
                                  "neg_days": 0, "credit_ids": [], "business_ids": [], "debit_ids": [],
                                  "cash_dep_ids": [], "cash_wdl_ids": [], "return_ids": [], "balance_ids": []})
    for d, b in daily.items():
        m = months[_month(d)]
        m["days"] += 1
        m["bal_sum"] += b
        m["min_bal"] = b if m["min_bal"] is None else min(m["min_bal"], b)
        if b < 0:
            m["neg_days"] += 1

    cash_dep, cash_wdl, returns, own, business = [], [], [], [], []
    neg_ids = []
    for t in rows:
        m = months[_month(date.fromisoformat(t["txn_date"]))]
        nar = t["narration"]
        if t["balance"] is not None:
            m["balance_ids"].append(t["id"])
            if t["balance"] < 0:
                neg_ids.append(t["id"])
        if t["credit"]:
            amt = D(str(t["credit"]))
            m["credits"] += amt
            m["credit_n"] += 1
            m["credit_ids"].append(t["id"])
            if RETURN.search(nar):
                returns.append(t)
                m["returns"] += 1
                m["return_ids"].append(t["id"])
                continue
            if CASH_DEP.search(nar):
                cash_dep.append(t)
                m["cash_dep"] += amt
                m["cash_dep_ids"].append(t["id"])
            elif SELF.search(nar):
                own.append(t)
                continue
            m["business_credits"] += amt
            m["business_ids"].append(t["id"])
            business.append(t)
        if t["debit"]:
            amt = D(str(t["debit"]))
            m["debits"] += amt
            m["debit_n"] += 1
            m["debit_ids"].append(t["id"])
            if RETURN.search(nar) and not (CHARGE.search(nar) and not re.search(r"\b(ECS|NACH|ACH|CHQ|CHEQUE)\b", nar, re.I)):
                returns.append(t)
                m["returns"] += 1
                m["return_ids"].append(t["id"])
            elif CASH_WDL.search(nar):
                cash_wdl.append(t)
                m["cash_wdl"] += amt
                m["cash_wdl_ids"].append(t["id"])

    # retention: large business credits matched by subsequent debits within QUICK_DAYS
    # Each rupee debited is allocated to one credit only (earliest credit first), so
    # one withdrawal cannot be matched against two credits.
    quick, large = [], []
    pool = [[date.fromisoformat(t["txn_date"]), D(str(t["debit"]))] for t in rows if t["debit"]]
    for t in sorted(business, key=lambda x: (x["txn_date"], x["seq"])):
        amt = D(str(t["credit"]))
        if amt < LARGE_CREDIT:
            continue
        large.append(t)
        d0 = date.fromisoformat(t["txn_date"])
        need, taken = amt * QUICK_SHARE, D(0)
        window = [p for p in pool if d0 <= p[0] <= d0 + timedelta(days=QUICK_DAYS) and p[1] > 0]
        if sum((p[1] for p in window), D(0)) >= need:
            for p in window:
                use = min(p[1], need - taken)
                p[1] -= use
                taken += use
                if taken >= need:
                    break
            quick.append(t)

    # concentration
    parties = defaultdict(lambda: {"amount": D(0), "ids": []})
    for t in business:
        if t in cash_dep:
            continue
        cp = counterparty(t["narration"]) or "UNIDENTIFIED"
        parties[cp]["amount"] += D(str(t["credit"]))
        parties[cp]["ids"].append(t["id"])
    noncash = sum((p["amount"] for p in parties.values()), D(0))
    top = sorted(parties.items(), key=lambda kv: -kv[1]["amount"])[:5]

    total_credits = sum((m["credits"] for m in months.values()), D(0))
    total_business = sum((m["business_credits"] for m in months.values()), D(0))
    total_cash_dep = sum((D(str(t["credit"])) for t in cash_dep), D(0))
    n_months = len([k for k, m in months.items() if m["days"] or m["credit_n"] or m["debit_n"]])
    month_rows = []
    for k in sorted(months):
        m = months[k]
        month_rows.append({"month": k, "amb": _f(m["bal_sum"] / m["days"]) if m["days"] else None,
                           "days": m["days"], "credits": _f(m["credits"]), "credit_n": m["credit_n"],
                           "business_credits": _f(m["business_credits"]), "debits": _f(m["debits"]),
                           "debit_n": m["debit_n"], "cash_dep": _f(m["cash_dep"]), "cash_wdl": _f(m["cash_wdl"]),
                           "returns": m["returns"], "min_balance": _f(m["min_bal"]), "neg_days": m["neg_days"],
                           "ids": {k: m[k + "_ids"] for k in ("credit", "business", "debit", "cash_dep",
                                                             "cash_wdl", "return", "balance")}})
    ambs = [r["amb"] for r in month_rows if r["amb"] is not None and r["days"] >= 20]
    quick_amt = sum((D(str(t["credit"])) for t in quick), D(0))
    large_amt = sum((D(str(t["credit"])) for t in large), D(0))
    return {
        "account_key": key, "account_type": acct_type,
        "from": start.isoformat(), "to": end.isoformat(), "months": month_rows, "month_count": n_months,
        "balance_ids": [i for r in month_rows for i in r["ids"]["balance"]],
        "business_ids": [t["id"] for t in business], "negative_ids": neg_ids,
        "amb_method": "Average of end-of-day balances over the days covered; months with fewer than 20 "
                      "covered days are left out of the average.",
        "amb": _f(sum((D(str(a)) for a in ambs), D(0)) / len(ambs)) if ambs else None,
        "balances_available": has_bal,
        "total_credits": _f(total_credits), "business_credits": _f(total_business),
        "avg_monthly_business_credits": _f(total_business / n_months) if n_months else None,
        "cash_deposits": {"amount": _f(total_cash_dep), "count": len(cash_dep), "ids": [t["id"] for t in cash_dep],
                          "share": _f(total_cash_dep / total_business) if total_business else None},
        "cash_withdrawals": {"amount": _f(sum((D(str(t["debit"])) for t in cash_wdl), D(0))),
                             "count": len(cash_wdl), "ids": [t["id"] for t in cash_wdl]},
        "returns": {"count": len(returns), "ids": [t["id"] for t in returns],
                    "rows": [{"id": t["id"], "date": t["txn_date"], "narration": t["narration"],
                              "amount": t["debit"] or t["credit"]} for t in returns]},
        "own_transfers": {"count": len(own), "ids": [t["id"] for t in own]},
        "negative_days": sum(r["neg_days"] for r in month_rows),
        "retention": {"large_credits": len(large), "quick_out": len(quick), "ids": [t["id"] for t in quick],
                      "share": _f(quick_amt / large_amt) if large_amt else None,
                      "rule": f"Credits of ₹{LARGE_CREDIT:,.0f} or more matched by at least "
                              f"{QUICK_SHARE * 100:.0f}% of their value in debits within {QUICK_DAYS} days; each "
                              "debit is counted against one credit only. A timing pattern, not money traced."},
        "concentration": {"top": [{"party": p, "amount": _f(v["amount"]),
                                   "share": _f(v["amount"] / noncash) if noncash else None,
                                   "count": len(v["ids"]), "ids": v["ids"]} for p, v in top],
                          "basis": "Non-cash business credits grouped by the counterparty name read from the "
                                   "narration (inferred; names are as the bank printed them)."},
        "review_thresholds": REVIEW,
    }


def _f(v):
    return None if v is None else float(D(v).quantize(D("0.01")))
