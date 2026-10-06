"""Illustrative eligibility arithmetic. Deterministic; no defaults.

There is deliberately no default ratio, rate or tenure: those are lender
policy, and a number chosen here would look like one. Missing inputs give
status 'needs_input'; impossible ones give 'invalid' with the reason.

    existing ratio  = obligations / income
    capacity (EMI)  = max(0, ratio_max × income − obligations)
    principal       = capacity × (1 − (1 + i)^−n) / i,  i = annual rate / 12
                    = capacity × n                       when the rate is 0
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

D = Decimal
LABEL = "Illustrative policy assumptions — not a lender approval or sanction."


def _dec(v):
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except (InvalidOperation, ValueError):
        return "bad"


def compute(*, income, obligations, foir_max_pct, annual_rate_pct, tenure_months) -> dict:
    inc, obl, ratio, rate, n = map(_dec, (income, obligations, foir_max_pct, annual_rate_pct,
                                          tenure_months))
    errors, missing = [], []
    for name, v in (("eligible monthly income", inc), ("included obligations", obl),
                    ("maximum obligation-to-income ratio", ratio), ("annual interest rate", rate),
                    ("tenure", n)):
        if v is None:
            missing.append(name)
        elif v == "bad":
            errors.append(f"{name.capitalize()} is not a number.")
    out = {"status": None, "errors": errors, "missing": missing, "label": LABEL,
           "existing_ratio_pct": None, "capacity_emi": None, "principal": None, "notes": []}
    if errors:
        out["status"] = "invalid"
        return out
    if inc is not None and inc <= 0:
        errors.append("Eligible monthly income must be greater than zero; no ratio can be computed.")
    if obl is not None and obl < 0:
        errors.append("Obligations cannot be negative.")
    if ratio is not None and not (D(0) < ratio <= D(100)):
        errors.append("The maximum ratio must be above 0% and at most 100%.")
    if rate is not None and not (D(0) <= rate <= D(60)):
        errors.append("The annual rate must be between 0% and 60%.")
    if n is not None and (n != n.to_integral_value() or not (1 <= n <= 480)):
        errors.append("Tenure must be a whole number of months between 1 and 480.")
    if errors:
        out["status"] = "invalid"
        return out

    if inc is not None and obl is not None:
        out["existing_ratio_pct"] = float((obl / inc * 100).quantize(D("0.01"), ROUND_HALF_UP))
    if missing:
        out["status"] = "needs_input"
        return out

    headroom = ratio / 100 * inc - obl
    capacity = max(D(0), headroom).quantize(D("0.01"), ROUND_HALF_UP)
    if headroom <= 0:
        out["notes"].append("Existing obligations already meet or exceed the illustrative ratio; "
                            "no additional EMI capacity at these assumptions.")
    i = rate / 100 / 12
    n_int = int(n)
    if i == 0:
        principal = capacity * n_int
        out["notes"].append("Zero interest rate: principal = EMI × tenure.")
    else:
        principal = capacity * (1 - (1 + i) ** -n_int) / i
    out.update(status="computed", capacity_emi=float(capacity),
               principal=float(principal.quantize(D("1"), ROUND_HALF_UP)),
               max_obligation=float((ratio / 100 * inc).quantize(D("0.01"), ROUND_HALF_UP)),
               monthly_rate=float(i))
    return out
