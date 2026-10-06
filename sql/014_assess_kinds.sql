-- Migration 014: Assess identifies more document types by content.
--   form16_part_a / form16_part_b / tax_statement (AIS, 26AS) / itr_ack /
--   kyc (PAN, Aadhaar, voter ID) / loan_sanction (non-OD sanction letters),
--   and status 'identified': the type is known, nothing is extracted (KYC
--   documents keep no page text at all). Idempotent.
ALTER TABLE assess.document DROP CONSTRAINT IF EXISTS document_kind_check;
ALTER TABLE assess.document ADD CONSTRAINT document_kind_check CHECK (kind IN (
    'salary_slip','bank_statement','credit_report','itr_json','od_sanction','other',
    'form16_part_a','form16_part_b','tax_statement','itr_ack','kyc','loan_sanction'));
ALTER TABLE assess.document DROP CONSTRAINT IF EXISTS document_status_check;
ALTER TABLE assess.document ADD CONSTRAINT document_status_check CHECK (status IN (
    'received','processing','complete','incomplete','failed','unsupported','identified'));
ALTER TABLE assess.document ADD COLUMN IF NOT EXISTS kind_basis TEXT;
ALTER TABLE assess.item DROP CONSTRAINT IF EXISTS item_item_type_check;
ALTER TABLE assess.item ADD CONSTRAINT item_item_type_check CHECK (item_type IN (
    'salary_slip','tradeline','credit_report','statement','itr','od_sanction','form16'));
