-- Migration 011: agency-adjusted figures are their own basis.
--
-- Crisil Ratings publishes key financial indicators "Crisil Ratings-adjusted":
-- the agency's restatement of the issuer's accounts, not the reported figures.
-- Compared with an annual report, a difference would read as a disagreement
-- when it is a definition. So such a claim carries basis 'agency_adjusted'
-- (consolidated or standalone stays in its fact key), and reconciliation never
-- compares a fact key whose claims mix adjusted and reported bases
-- (reconcile.py). Adjusted figures are compared only with adjusted figures.
--
-- Idempotent.

BEGIN;

-- A key financial indicator (total assets, PAT, GNPA %, gearing times) is its
-- own kind of claim: none of the existing types is a financial ratio.
ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_claim_type_check;
ALTER TABLE claim ADD CONSTRAINT claim_claim_type_check
    CHECK (claim_type IN ('total_borrowings', 'debt_instrument', 'rating',
                          'rationale_point', 'rating_history', 'financial_indicator'));

ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_basis_check;
ALTER TABLE claim ADD CONSTRAINT claim_basis_check
    CHECK (basis IN ('standalone', 'consolidated', 'unknown', 'agency_adjusted'));

-- What a rating says about itself beyond grade and outlook, as the publisher
-- marks it: 'ppmld' (principal-protected market-linked), 'legacy_r' (the old
-- " r" suffix), 'interchangeable_subordinated', 'retail', 'not_yet_issued'.
-- Informational: comparison is on grade, outlook and watch.
ALTER TABLE rating_action
    ADD COLUMN IF NOT EXISTS qualifiers TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
