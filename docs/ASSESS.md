# IssuerGraph Assess (local demo)

A private, case-scoped workflow for one salaried borrower:
documents → extraction → source-linked facts → liability reconciliation →
analyst review → illustrative eligibility → Excel export.

It is a separate surface from the issuer product: its own Postgres schema
(`assess`), its own routes (`/assess`, `/api/assess/*`), served to loopback
clients only and not at all when `ISSUERGRAPH_DEMO_ONLY=1`. Applicant data
never enters the issuer catalogue, `/demo`, the snapshot or analytics.

## Run it

```bash
uv pip install --python .venv/bin/python -r requirements.txt     # adds openpyxl, boto3[crt]
psql -d issuergraph -f sql/013_assess.sql                        # idempotent
.venv/bin/python -m issuergraph.assess synthetic                 # labelled synthetic case
.venv/bin/uvicorn issuergraph.api:app --host 127.0.0.1 --port 8077
open http://127.0.0.1:8077/assess
```

A real case:

```bash
# optional: allow Textract (scanned pages) and Bedrock (credit report, sanction letter)
aws login --profile issuergraph
export ASSESS_AWS_PROFILE=issuergraph            # same shell as uvicorn / the CLI

.venv/bin/python -m issuergraph.assess new --label "Salaried — OD" --name "Applicant name"
.venv/bin/python -m issuergraph.assess import <case_id> /path/to/folder   # PDFs + ITR JSON, no recursion
.venv/bin/python -m issuergraph.assess process <case_id>
.venv/bin/python -m issuergraph.assess unlock <doc_id>     # password-protected PDF (prompts)
```

Or create the case and upload files in the UI. The CLI prints ids and
statuses only, never document content.

Files are copied to `data/assess/case_<id>/` (gitignored, mode 0600).
Results persist: reopening a case does not repeat OCR or model calls;
"Re-run" on a document does.

## What is read, and how

| Document | Reader | Evidence |
|---|---|---|
| Bank statement (digital PDF) | `bank.py`: per-page header detection, word geometry, wrapped narrations, Dr/Cr and minus balances | every row anchored to its words; highlighted |
| Salary slip | `extract.slip_deterministic` (label → amount on the same row); model only for missing fields | highlighted |
| Credit report — CIBIL (myscore print-out) | `cibil.py`, deterministic: label/value rows in visual order, one account per Member Name…Account Number block, enquiries excluded; account count must equal the report's own | highlighted |
| Credit report — other layouts | model (Bedrock), whole report in one call when it fits; each field needs a verbatim quote that is then found on a page; account count checked | verified → highlighted; not found → *unverified*, quote shown, excluded from totals |
| Form 16 Part B | `form16.py`, by printed label (TRACES layout) | highlighted |
| KYC, AIS/26AS, Form 16 Part A, ITR-V, non-OD sanction letters | identified only (`classify.py`); KYC keeps no text | — |
| OD sanction letter | model, same verification | same |
| ITR JSON | parsed directly; return-format vs prefill told apart | JSON path |
| Scanned page | Textract `DetectDocumentText` → same word-map format | OCR boxes highlighted |

Checks: opening + credits − debits = closing, row-by-row running balance,
the statement's own summary counts/totals, rows outside the stated period,
overlapping statements of the same account (de-duplicated across documents).

Document type is read from content (`classify.py`), never from the filename
alone; the analyst can override it. Statement layouts: HDFC-style (synthetic)
and ICICI detailed statements (ruled rows, stacked headers) — verified on a
real 10-page statement, every running balance consistent.

Model choice (account 173639292018): Anthropic models need the Bedrock
use-case form, not yet submitted. `openai.gpt-oss-120b-1:0` runs on-demand in
ap-south-1 (data stays in India) and scored 24/24 on the synthetic report:
`ASSESS_BEDROCK_MODEL=openai.gpt-oss-120b-1:0`.

## Rules the code enforces

- A bureau account and the bank debit that repays it are **one** obligation.
  An unmatched recurring loan-like debit is surfaced for a decision, not added.
- An OD's sanctioned limit is never debt or EMI. Its obligation is either an
  analyst-entered amount with a basis, or explicitly excluded; until then the
  row is unresolved and the result provisional.
- "Reported blank" and "not reported" are never zero.
- Model page numbers are not trusted: a quote is searched for; a unique hit on
  another page is used and noted.
- Corrections, decisions and assumptions are appended, never overwrite the
  extraction; the newest wins and every figure is recomputed (`analysis.build`).
- No default ratio, rate or tenure. Missing → "Needs input"; impossible →
  "invalid"; unresolved blocking items → "provisional".
- The workbook is built from the same `analysis.build` output as the UI.

## Limits (demo, not production)

- Salaried borrowers only; one statement layout family verified (HDFC-style
  netbanking PDF, on synthetic fixtures). Other banks may parse if their
  header uses the same vocabulary — check the reconciliation result.
- Statements whose amounts sit on the middle line of a multi-line row
  (some ICICI/SBI exports) are not handled.
- Credit reports need a model; without `ASSESS_AWS_PROFILE` they fail visibly.
- No authentication or multi-tenancy: loopback only, one local user.
- No live bureau, bank-portal, lender or payment integration.
- Passing reconciliation is not evidence that a document is authentic.

## Proprietorship (business loan) — demo scope

`python -m issuergraph.assess synthetic-proprietorship` builds a labelled synthetic
case (12-month current account, savings account, GST and Udyam certificates);
`new --type proprietorship` or the New case dialog starts a real one.

- Checklist from the advisor's process note: KYC, GST (or alternative proof),
  Udyam, 2 years' ITR with computation, P&L/BS, ownership proof, 12 months of
  current account, 6 months of savings account, loan SOA if loans are seen.
- Business profile: GST REG-06 and Udyam certificates read deterministically
  (`registration.py`); vintage = earliest registration/commencement date read.
- Banking analysis (`banking.py`), per account, every figure linked to its
  transactions: AMB (end-of-day average), business credits by month, cash
  deposits/withdrawals, cheque/ECS/NACH returns, days below zero, large credits
  moved out within 2 days, payer concentration (inferred from narrations).
  Findings use stated review prompts, not lender rules.
- Not yet: GST return analysis (needs GSTR data), ITR computation and P&L/BS
  extraction, GST vs ITR vs bank turnover consistency — pilot scope.

## Public read-only demo (`/demo/assess`)

The live site shows the synthetic cases only, from a committed static snapshot:

```bash
.venv/bin/python -m scripts.export_assess_demo        # every case flagged is_synthetic
.venv/bin/python -m pytest tests/test_assess.py -q
git add static/demo/assess && git commit
```

The exporter refuses any case not flagged synthetic, re-verifies every anchor
against its page text, renders the referenced pages and draws highlights from
the stored rectangles. In the demo, corrections, decisions and uploads show a
read-only notice; the review pack is a pre-built sample workbook.
