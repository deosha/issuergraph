-- Migration 010: evidence anchored in HTML, not only in PDF pages.
--
-- Some publishers (CRISIL) issue rationales as HTML pages. There is no page and
-- no word geometry, so an HTML anchor locates its text by a node path — lxml's
-- getpath() on the parsed raw HTML — and a character range inside that node's
-- text_content(), exactly as lxml returns it, whitespace and all.
--
--   * document.media_type says which kind of source a document is, and
--     content_type keeps the HTTP Content-Type header it was served with: the
--     charset needed to decode the bytes the same way every time lives there,
--     not always in the file.
--
--   * document_blob holds an HTML document's bytes exactly as received. The
--     loader verifies anchors against these bytes — re-hashing them against
--     document.sha256 and re-parsing them — rather than against a local file
--     that could change underneath it. A PDF is verified against its extracted
--     page text (document_page), so its bytes stay on disk as before.
--
--   * evidence_anchor.kind is 'pdf' (page_no + offsets into document_page.text,
--     with rectangles) or 'html' (node_path + offsets into that node's text).
--     Each kind carries exactly its own locator.
--
-- Idempotent.

BEGIN;

ALTER TABLE document
    ADD COLUMN IF NOT EXISTS media_type   TEXT NOT NULL DEFAULT 'application/pdf',
    ADD COLUMN IF NOT EXISTS content_type TEXT;
ALTER TABLE document DROP CONSTRAINT IF EXISTS document_media_type_check;
ALTER TABLE document ADD CONSTRAINT document_media_type_check
    CHECK (media_type IN ('application/pdf', 'text/html'));

CREATE TABLE IF NOT EXISTS document_blob (
    document_id BIGINT PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    bytes       BYTEA NOT NULL
);

ALTER TABLE evidence_anchor
    ADD COLUMN IF NOT EXISTS kind      TEXT NOT NULL DEFAULT 'pdf',
    ADD COLUMN IF NOT EXISTS node_path TEXT;
ALTER TABLE evidence_anchor ALTER COLUMN page_no DROP NOT NULL;
ALTER TABLE evidence_anchor DROP CONSTRAINT IF EXISTS evidence_anchor_locator_check;
ALTER TABLE evidence_anchor ADD CONSTRAINT evidence_anchor_locator_check
    CHECK ((kind = 'pdf'  AND page_no IS NOT NULL AND node_path IS NULL)
        OR (kind = 'html' AND node_path IS NOT NULL AND page_no IS NULL
                          AND bbox IS NULL AND bbox_rects IS NULL));

COMMIT;
