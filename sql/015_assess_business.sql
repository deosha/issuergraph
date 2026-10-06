-- Migration 015: proprietorship documents. Idempotent.
--   gst_certificate / udyam_certificate: registration dates are read (vintage);
--   financials (P&L, balance sheet), itr_computation, business_proof (utility
--   bill etc.), loan_soa: identified for the checklist, not extracted.
ALTER TABLE assess.document DROP CONSTRAINT IF EXISTS document_kind_check;
ALTER TABLE assess.document ADD CONSTRAINT document_kind_check CHECK (kind IN (
    'salary_slip','bank_statement','credit_report','itr_json','od_sanction','other',
    'form16_part_a','form16_part_b','tax_statement','itr_ack','kyc','loan_sanction',
    'gst_certificate','udyam_certificate','financials','itr_computation','business_proof','loan_soa'));
ALTER TABLE assess.item DROP CONSTRAINT IF EXISTS item_item_type_check;
ALTER TABLE assess.item ADD CONSTRAINT item_item_type_check CHECK (item_type IN (
    'salary_slip','tradeline','credit_report','statement','itr','od_sanction','form16','registration'));
