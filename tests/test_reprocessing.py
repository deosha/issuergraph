"""An extractor fix has to reach documents that are already ingested.

The pipeline used to skip any document that had claims, so a correction applied
only to issuers loaded after it and the two populations diverged silently. These
tests drive `extraction_needed` — the decision itself — and then the real
pipeline against a document whose claims are marked as coming from an older
extractor version, which is exactly the state a shipped fix creates.
"""
from __future__ import annotations

import pytest

from issuergraph import pipeline
from issuergraph.corpus import SOURCES
from issuergraph.db import connect, one, query
from issuergraph.extractors import annual_report as ar

STALE = "0.0.1-test"


@pytest.fixture
def annual_report_document():
    row = one(
        "SELECT id FROM document WHERE doc_type = 'annual_report' ORDER BY id LIMIT 1"
    )
    if not row:
        pytest.skip("annual report not ingested — run the pipeline first")
    return row["id"]


@pytest.fixture
def stale_claims(annual_report_document):
    """Mark the document's claims as produced by an older extractor version."""
    with connect() as conn:
        conn.execute(
            "UPDATE claim SET extractor_version = %s WHERE document_id = %s",
            (STALE, annual_report_document),
        )
        conn.commit()
    yield annual_report_document
    pipeline.run()          # restores the current version by re-extracting


# --- the decision -----------------------------------------------------------

def test_matching_version_is_skipped(annual_report_document):
    with connect() as conn:
        reason, count, version = pipeline.extraction_needed(
            conn, annual_report_document, ar)
    assert reason is None
    assert count and version == ar.VERSION


def test_changed_version_triggers_reprocessing(stale_claims):
    with connect() as conn:
        reason, count, version = pipeline.extraction_needed(conn, stale_claims, ar)
    assert reason == "version_changed"
    assert version == STALE and count == 16


def test_force_reprocesses_a_current_document(annual_report_document):
    with connect() as conn:
        reason, _, _ = pipeline.extraction_needed(
            conn, annual_report_document, ar, force=True)
    assert reason == "forced"


def test_a_document_with_no_claims_is_initial():
    with connect() as conn:
        reason, count, version = pipeline.extraction_needed(conn, -1, ar)
    assert reason == "initial" and count == 0 and version is None


# --- the effect -------------------------------------------------------------

def test_the_pipeline_re_extracts_a_stale_document(stale_claims):
    result = pipeline.run()
    assert [r["file"] for r in result["reprocessed"]] == ["ar2025.pdf"]
    versions = query(
        "SELECT DISTINCT extractor_version FROM claim WHERE document_id = %s",
        (stale_claims,),
    )
    assert [v["extractor_version"] for v in versions] == [ar.VERSION]


def test_reprocessing_replaces_claims_rather_than_duplicating(stale_claims):
    before = one("SELECT count(*) AS n FROM claim WHERE document_id = %s",
                 (stale_claims,))["n"]
    pipeline.run()
    after = one("SELECT count(*) AS n FROM claim WHERE document_id = %s",
                (stale_claims,))["n"]
    assert after == before, "a reprocess must not stack two parser generations"


def test_reprocessing_is_logged_with_both_versions(stale_claims):
    pipeline.run()
    run = one(
        "SELECT * FROM extraction_run WHERE document_id = %s ORDER BY id DESC LIMIT 1",
        (stale_claims,),
    )
    assert run["reason"] == "version_changed"
    assert run["previous_version"] == STALE
    assert run["extractor_version"] == ar.VERSION
    assert run["claims_replaced"] == 16 and run["claims_written"] == 16


def test_conflict_history_survives_reprocessing(stale_claims):
    """Claims are derived and replaceable; when a conflict was first seen is not.

    The claim ids underneath a conflict all change during a reprocess, because
    conflict_member cascades with the claims. The conflict row is upserted on
    (issuer_id, fact_key, kind), so first_detected_at has to survive that.
    """
    key = "total_borrowings|consolidated|2024-03-31"
    before = one("SELECT id, first_detected_at FROM conflict WHERE fact_key = %s", (key,))
    if not before:
        pytest.skip("expected conflict not present — run the pipeline first")
    members_before = {m["claim_id"] for m in query(
        "SELECT claim_id FROM conflict_member WHERE conflict_id = %s", (before["id"],))}

    pipeline.run()

    after = one("SELECT id, first_detected_at, last_seen_at FROM conflict "
                "WHERE fact_key = %s", (key,))
    assert after["first_detected_at"] == before["first_detected_at"]
    assert after["last_seen_at"] >= before["first_detected_at"]

    members_after = {m["claim_id"] for m in query(
        "SELECT claim_id FROM conflict_member WHERE conflict_id = %s", (after["id"],))}
    assert len(members_after) == len(members_before)
    assert members_after != members_before, "the reprocess should have new claim ids"


def test_every_document_still_has_exactly_one_extractor_version():
    rows = query(
        """
        SELECT document_id, count(DISTINCT extractor_version) AS versions
        FROM claim GROUP BY document_id HAVING count(DISTINCT extractor_version) > 1
        """
    )
    assert rows == [], f"documents carrying mixed parser generations: {rows}"


def test_shipped_extractor_versions_match_what_is_stored():
    """The condition the skip logic depends on: no drift between code and data."""
    for source in SOURCES:
        stored = query(
            """
            SELECT DISTINCT c.extractor_version FROM claim c
            JOIN document d ON d.id = c.document_id
            WHERE c.extractor = %s
            """,
            (source.extractor.EXTRACTOR,),
        )
        assert [s["extractor_version"] for s in stored] in ([], [source.extractor.VERSION]), \
            f"{source.extractor.EXTRACTOR} has claims from another version"
