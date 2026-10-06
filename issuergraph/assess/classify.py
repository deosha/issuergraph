"""What a document is, read from its content — never from its filename alone.

Rules are ordered so that a document mentioning another's vocabulary is not
misread: a credit report mentions "sanction", an annual tax statement lists
withdrawals and deposits, a Form 16 mentions TDS. The bank-statement test is
structural (a transaction-table header the parser recognises), not lexical.
The filename is used only when the content says nothing (no text layer).

Returns (kind, basis) — the basis is shown to the analyst, who can override.
"""
from __future__ import annotations

import re

from . import bank
from .pages import Page

KYC_MAX_CHARS = 1500     # identity documents are short; a long document naming PAN is not one


def classify(pages: list[Page], filename: str = "") -> tuple[str, str]:
    text = "\n".join(p.text for p in pages[:4])
    low = " ".join(text.lower().split())
    n = len(low)

    if not low.strip():
        return _by_name(filename, "No text layer; type guessed from the filename only.")
    if ("aadhaar" in low or "unique identification authority" in low) and n < KYC_MAX_CHARS:
        return "kyc", "Identity document (Aadhaar)."
    if ("election commission" in low or "elector" in low) and n < KYC_MAX_CHARS:
        return "kyc", "Identity document (voter ID)."
    if "permanent account number" in low and n < KYC_MAX_CHARS:
        return "kyc", "Identity document (PAN card)."
    if "gst reg-06" in low or ("registration certificate" in low and ("gstin" in low or "goods and services tax" in low)):
        return "gst_certificate", "GST registration certificate."
    if "udyam registration" in low and ("certificate" in low or "udyam-" in low):
        return "udyam_certificate", "Udyam (MSME) registration certificate."
    if any(k in low for k in ("computation of total income", "statement of total income",
                              "computation of income")):
        return "itr_computation", "Income-tax computation."
    if "balance sheet" in low and ("profit and loss" in low or "profit & loss" in low):
        return "financials", "Financial statements (P&L and balance sheet)."
    if any(k in low for k in ("annual tax statement", "form 26as", "annual information statement")):
        return "tax_statement", "Annual tax statement (AIS / 26AS)."
    if "itr-v" in low or "income tax return acknowledgement" in low or \
            "indian income tax return acknowledgement" in low:
        return "itr_ack", "Income-tax return acknowledgement (ITR-V)."
    if ("form no. 16" in low or "form 16" in low) and "section 203" in low:
        if "gross total income" in low or "income chargeable under the head" in low:
            return "form16_part_b", "Form 16 Part B (salary and tax computation)."
        return "form16_part_a", "Form 16 Part A (TDS certificate)."
    if re.search(r"\b(cibil|transunion|experian|equifax|crif|credit bureau|credit report|credit information report)\b", low) \
            and len(pages) >= 2 and \
            sum(k in low for k in ("credit score", "account type", "date reported", "days past due",
                                   "date opened", "ownership")) >= 2:
        return "credit_report", "Credit bureau report."
    if _has_statement_header(pages):
        return "bank_statement", "Transaction table with date, withdrawal/deposit and balance columns."
    if ("loan account" in low or "repayment schedule" in low) and \
            any(k in low for k in ("statement of account", "soa", "principal", "emi")):
        return "loan_soa", "Loan statement of account / repayment schedule."
    if ("electricity" in low and ("bill" in low or "consumer" in low)) or \
            any(k in low for k in ("shop and establishment", "shops and establishment", "trade licence",
                                   "trade license")):
        return "business_proof", "Business address / licence proof."
    if re.search(r"\b(pay ?slip|salary slip)\b", low) or \
            ("net pay" in low and "earnings" in low and "deduction" in low):
        return "salary_slip", "Salary slip (earnings, deductions, net pay)."
    if "sanction" in low:
        if "overdraft" in low or "drawing power" in low:
            return "od_sanction", "Overdraft sanction letter."
        return "loan_sanction", "Loan sanction letter" + (" (in-principle)." if "in-principle" in low
                                                          or "in principle" in low else ".")
    return _by_name(filename, "Content did not match a supported type.")


def _has_statement_header(pages: list[Page]) -> bool:
    for page in pages[:2]:
        rows = bank.visual_rows(page.words())
        for i, row in enumerate(rows):
            if bank._header_columns(row) or bank._stacked_header(rows, i):
                return True
    return False


def _by_name(filename: str, why: str) -> tuple[str, str]:
    n = filename.lower()
    for words, kind in ((("slip", "payslip"), "salary_slip"), (("statement", "stmt"), "bank_statement"),
                        (("cibil", "credit report", "bureau"), "credit_report"),
                        (("sanction",), "loan_sanction")):
        if any(w in n for w in words):
            return kind, why + f" Filename suggests {kind.replace('_', ' ')} — confirm."
    return "other", why
