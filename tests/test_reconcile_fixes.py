"""Regression tests for the reconciliation and loader fixes.

Each test names the defect it locks down. The loader tests build a throwaway
issuer/document/page so they can feed deliberately broken anchors without
touching the real corpus; everything is rolled back.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from issuergraph.db import connect, one, query
from issuergraph.loader import EvidenceMismatch, load_claims
from issuergraph.models import Anchor, ExtractedClaim, normalize_value
from issuergraph.reconcile import (EXCLUDED_KEYS, TOLERANCE_ABS_CR, TOLERANCE_PCT,
                                   _describe, _excluded, disagrees, reconcile)

PAGE_TEXT = "Total borrowings\n51,068.03\nas at March 31, 2025"


@pytest.fixture
def conn():
    with connect() as c:
        yield c


@pytest.fixture
def sandbox(conn):
    """A scratch issuer + document + page, rolled back at the end of the test."""
    with conn.transaction() as tx:
        issuer_id = conn.execute(
            "INSERT INTO issuer (name) VALUES ('Fixture Issuer') RETURNING id"
        ).fetchone()["id"]
        document_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, page_count)
            VALUES (%s,'annual_report','Fixture','t','u','deadbeef',1,'/dev/null',now(),1)
            RETURNING id
            """,
            (issuer_id,),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO document_page (document_id, page_no, text, width, height, word_map)
            VALUES (%s, 1, %s, 100, 100, %s)
            """,
            (document_id, PAGE_TEXT,
             psycopg.types.json.Jsonb(
                 [[0, 5, 0, 0, 10, 8], [6, 16, 11, 0, 30, 8], [17, 26, 0, 10, 20, 18]])),
        )
        yield {"issuer_id": issuer_id, "document_id": document_id}
        raise psycopg.Rollback(tx)


def _claim(**anchor_kwargs) -> ExtractedClaim:
    defaults = dict(page_no=1, char_start=17, char_end=26, evidence_text="51,068.03")
    return ExtractedClaim(
        claim_type="total_borrowings", fact_key="fixture|total", subject="fixture",
        value_numeric=Decimal("51068.03"), value_unit="INR_CRORE",
        extractor="fixture", extractor_version="1.0.0",
        anchors=[Anchor(**{**defaults, **anchor_kwargs})],
    )


# --- 1. offsets outside the page must not pass the slice comparison ----------

def test_offsets_past_end_of_page_are_rejected(conn, sandbox):
    """Python slicing truncates silently: text[99999:99999] == "" on any page."""
    claim = _claim(char_start=99999, char_end=100000, evidence_text="anything")
    with pytest.raises(EvidenceMismatch, match="past the end"):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [claim])


def test_negative_offsets_are_rejected(conn, sandbox):
    """A negative char_start slices from the end and can coincidentally match."""
    claim = _claim()
    # set past construction, the way a buggy extractor's arithmetic would
    claim.anchors[0].char_start = -9        # text[-9:] happens to equal the tail
    claim.anchors[0].char_end = len(PAGE_TEXT)
    claim.anchors[0].evidence_text = PAGE_TEXT[-9:]
    with pytest.raises(EvidenceMismatch, match="not a forward range"):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [claim])


def test_empty_evidence_text_is_rejected(conn, sandbox):
    claim = _claim()
    claim.anchors[0].evidence_text = ""     # set post-validation, as a bad extractor would
    with pytest.raises(EvidenceMismatch, match="empty evidence_text"):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [claim])


def test_range_covering_no_word_boxes_is_rejected(conn, sandbox):
    """A range landing in inter-word whitespace would highlight nothing."""
    claim = _claim()
    # char 5 is the space between the two words in the fixture's word_map
    claim.anchors[0].char_start, claim.anchors[0].char_end = 5, 6
    claim.anchors[0].evidence_text = PAGE_TEXT[5:6]
    with pytest.raises(EvidenceMismatch, match="no word boxes"):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [claim])


# --- 2. a failed load must leave nothing behind ------------------------------

def test_failed_load_rolls_back_earlier_claims(conn, sandbox):
    good = _claim()
    bad = _claim(char_start=99999, char_end=100000, evidence_text="nope")
    with pytest.raises(EvidenceMismatch):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [good, bad])
    left = conn.execute("SELECT count(*) AS n FROM claim WHERE document_id = %s",
                        (sandbox["document_id"],)).fetchone()["n"]
    assert left == 0, "the valid claim ahead of the failure was still committed"


def test_autocommit_connection_is_refused(sandbox):
    import psycopg
    from issuergraph.db import DSN
    with psycopg.connect(DSN, autocommit=True) as autoconn:
        with pytest.raises(EvidenceMismatch, match="autocommit"):
            load_claims(autoconn, sandbox["issuer_id"], sandbox["document_id"], [_claim()])


def test_reloading_a_document_replaces_rather_than_duplicates(conn, sandbox):
    for _ in range(3):
        load_claims(conn, sandbox["issuer_id"], sandbox["document_id"], [_claim()])
    n = conn.execute("SELECT count(*) AS n FROM claim WHERE document_id = %s",
                     (sandbox["document_id"],)).fetchone()["n"]
    assert n == 1


# --- 3. one document cannot conflict with itself -----------------------------

def test_two_ratings_in_one_document_are_not_a_conflict(conn, sandbox):
    """CRISIL publishes AA / AA- / PPMLD AA for different instruments in one action."""
    issuer_id, document_id = sandbox["issuer_id"], sandbox["document_id"]
    for value in ("AA", "AA-"):
        claim = ExtractedClaim(
            claim_type="rating", fact_key="rating_grade|long_term|2025Q3",
            subject="grade", value_text=value,
            extractor="fixture", extractor_version="1.0.0",
            anchors=[Anchor(page_no=1, char_start=0, char_end=5,
                            evidence_text=PAGE_TEXT[0:5])],
        )
        conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_text, normalized_value, extractor, extractor_version)
            VALUES (%s,%s,'rating',%s,'grade',%s,%s,'fixture','1.0.0')
            """,
            (issuer_id, document_id, claim.fact_key, value, normalize_value(value)),
        )
    result = reconcile(conn, issuer_id)
    assert result["conflicts"] == 0


def test_one_agency_changing_its_own_mind_is_not_a_conflict(conn, sandbox):
    """Two ICRA reports, different outlooks: a rating action, not a disagreement."""
    issuer_id = sandbox["issuer_id"]
    for value in ("Stable", "Negative"):
        doc_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, page_count)
            VALUES (%s,'rating_rationale','ICRA','t','u',%s,1,'/dev/null',now(),1) RETURNING id
            """,
            (issuer_id, f"hash-{value}"),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_text, normalized_value, extractor, extractor_version)
            VALUES (%s,%s,'rating','rating_outlook|long_term|2025Q3','outlook',%s,%s,'f','1')
            """,
            (issuer_id, doc_id, value, normalize_value(value)),
        )
    assert reconcile(conn, issuer_id)["conflicts"] == 0


def test_existing_conflicts_all_span_two_documents_and_two_sources(conn):
    """The four real conflicts were not false positives under the old logic."""
    rows = query(
        """
        SELECT k.fact_key, count(DISTINCT c.document_id) AS docs,
               count(DISTINCT d.source_name) AS sources
        FROM conflict k
        JOIN conflict_member m ON m.conflict_id = k.id
        JOIN claim c ON c.id = m.claim_id
        JOIN document d ON d.id = c.document_id
        GROUP BY k.fact_key
        """
    )
    assert rows, "expected the IIFL corpus to be loaded"
    for row in rows:
        assert row["docs"] > 1 and row["sources"] > 1, row


def test_another_issuers_claims_never_join_a_conflict(conn, sandbox):
    """fact_key is unique only within an issuer; every rated company has
    'rating_outlook|long_term|2025Q3'."""
    issuer_id = sandbox["issuer_id"]
    for source, value in (("ICRA", "Stable"), ("CARE", "Stable")):
        doc_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, page_count)
            VALUES (%s,'rating_rationale',%s,'t','u',%s,1,'/dev/null',now(),1) RETURNING id
            """,
            (issuer_id, source, f"other-{source}"),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_text, normalized_value, extractor, extractor_version)
            VALUES (%s,%s,'rating','rating_outlook|long_term|2025Q3','outlook',%s,%s,'f','1')
            """,
            (issuer_id, doc_id, value, normalize_value(value)),
        )
    # IIFL genuinely disagrees on this same fact_key; it must not bleed in here
    assert reconcile(conn, issuer_id)["conflicts"] == 0
    members = conn.execute(
        """
        SELECT c.issuer_id FROM conflict k
        JOIN conflict_member m ON m.conflict_id = k.id
        JOIN claim c ON c.id = m.claim_id
        WHERE k.issuer_id = %s
        """,
        (issuer_id,),
    ).fetchall()
    assert all(m["issuer_id"] == issuer_id for m in members)


# --- 4. a source holding two values must not collapse ------------------------

def test_note_keeps_both_values_from_one_source():
    rows = [
        {"source_name": "ICRA", "value_text": "AA"},
        {"source_name": "ICRA", "value_text": "AA-"},
        {"source_name": "CARE", "value_text": "AA"},
    ]
    note = _describe(rows)
    assert "ICRA says AA / AA-" in note and "CARE says AA" in note


# --- 5. conflict history survives a re-run -----------------------------------

def test_conflict_history_is_preserved_across_runs(conn):
    before = {r["fact_key"]: r for r in query(
        "SELECT fact_key, first_detected_at, last_seen_at FROM conflict")}
    assert before, "expected the IIFL corpus to be loaded"

    issuer_id = one("SELECT id FROM issuer WHERE name = 'IIFL Finance Limited'")["id"]
    with conn.transaction() as tx:
        reconcile(conn, issuer_id)
        after = {r["fact_key"]: r for r in conn.execute(
            "SELECT fact_key, first_detected_at, last_seen_at FROM conflict").fetchall()}
        for key, row in before.items():
            assert after[key]["first_detected_at"] == row["first_detected_at"], \
                f"{key}: first_detected_at was overwritten"
            assert after[key]["last_seen_at"] >= row["last_seen_at"]
        raise psycopg.Rollback(tx)


def test_a_conflict_that_stops_recurring_is_resolved_not_deleted(conn, sandbox):
    issuer_id = sandbox["issuer_id"]
    conn.execute(
        """
        INSERT INTO conflict (issuer_id, fact_key, subject, kind, note)
        VALUES (%s, 'stale|key', 'gone', 'numeric_disagreement', 'from a previous run')
        """,
        (issuer_id,),
    )
    result = reconcile(conn, issuer_id)
    assert result["resolved"] == 1
    row = conn.execute(
        "SELECT resolved_at FROM conflict WHERE issuer_id = %s AND fact_key = 'stale|key'",
        (issuer_id,),
    ).fetchone()
    assert row is not None and row["resolved_at"] is not None


# --- 6. new numeric fact types are reconciled by default ---------------------

def test_new_numeric_fact_type_is_reconciled_without_registration(conn, sandbox):
    """net_worth| is in no allowlist; it must still be compared."""
    issuer_id = sandbox["issuer_id"]
    for source, value in (("ICRA", Decimal("14241")), ("CARE", Decimal("13000"))):
        doc_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, page_count)
            VALUES (%s,'rating_rationale',%s,'t','u',%s,1,'/dev/null',now(),1) RETURNING id
            """,
            (issuer_id, source, f"hash-{source}"),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_numeric, value_unit, extractor, extractor_version)
            VALUES (%s,%s,'total_borrowings','net_worth|consolidated|2025-06-30','net worth',
                    %s,'INR_CRORE','f','1')
            """,
            (issuer_id, doc_id, value),
        )
    result = reconcile(conn, issuer_id)
    assert result["conflicts"] == 1
    row = conn.execute(
        "SELECT kind FROM conflict WHERE issuer_id = %s AND fact_key = %s",
        (issuer_id, "net_worth|consolidated|2025-06-30"),
    ).fetchone()
    assert row["kind"] == "numeric_disagreement"


def test_excluded_keys_are_named_with_a_reason():
    assert _excluded("instrument|INE530B07203|2032-03-24|60.00")
    assert _excluded("rated_amount|ICRA|ncd|1400.00") is not None
    assert _excluded("total_borrowings|consolidated|2025-03-31") is None
    for prefix, reason in EXCLUDED_KEYS:
        assert reason and len(reason) > 20, f"{prefix} needs a stated reason"


# --- 8. a percentage band alone mislabels the same gap on different bases -----

def test_the_two_fy24_gaps_are_both_conflicts(conn):
    """₹24.80 Cr consolidated and ₹25.10 Cr standalone are one definitional
    difference. A percentage-only test flagged the second and passed the first."""
    for fact_key in ("total_borrowings|consolidated|2024-03-31",
                     "total_borrowings|standalone|2024-03-31"):
        row = one("SELECT spread_pct, note FROM conflict WHERE fact_key = %s", (fact_key,))
        assert row is not None, f"{fact_key} should be a conflict"
    consolidated = one("SELECT spread_pct, note FROM conflict WHERE fact_key = %s",
                       ("total_borrowings|consolidated|2024-03-31",))
    assert consolidated["spread_pct"] < TOLERANCE_PCT, \
        "this one is caught by the absolute floor, not the percentage band"
    assert "₹5 crore tolerance" in consolidated["note"]


@pytest.mark.parametrize("lo,hi,unit,expected", [
    # the real pair, on their real bases
    (Decimal("46674.20"), Decimal("46699"), "INR_CRORE", True),   # 0.053%, ₹24.80 Cr
    (Decimal("19985.90"), Decimal("20011"), "INR_CRORE", True),   # 0.126%, ₹25.10 Cr
    # genuine rounding on a large base stays agreement
    (Decimal("51068.00"), Decimal("51068.03"), "INR_CRORE", False),
    (Decimal("24524.00"), Decimal("24524.16"), "INR_CRORE", False),
    # small base, big relative gap: still caught by the percentage band
    (Decimal("100"), Decimal("101"), "INR_CRORE", True),
    # gap of 6 on a huge base: under the percentage band, over the absolute one
    (Decimal("100000"), Decimal("100006"), "INR_CRORE", True),
    # ...but the absolute floor is a rupee amount, so it cannot apply here
    (Decimal("100000"), Decimal("100006"), "PERCENT", False),
])
def test_dual_tolerance(lo, hi, unit, expected):
    is_conflict, _, _ = disagrees(lo, hi, unit)
    assert is_conflict is expected


def test_absolute_floor_only_applies_to_rupee_units():
    """"6 over tolerance" means ₹6 crore. It cannot mean 6 percentage points.

    Same numbers, same relative spread (0.006%, inside the percentage band) —
    only the unit differs, and only the rupee one is a conflict.
    """
    lo, hi = Decimal("100000"), Decimal("100006")
    in_crore, spread_crore, why = disagrees(lo, hi, "INR_CRORE")
    in_percent, spread_percent, _ = disagrees(lo, hi, "PERCENT")

    assert spread_crore == spread_percent < TOLERANCE_PCT
    assert in_crore is True and "₹5 crore tolerance" in why
    assert in_percent is False


def test_corroboration_reports_its_residual_variance(conn):
    """Agreement within tolerance is not an exact match; the gap must survive."""
    rows = query(
        """
        SELECT c.fact_key, max(c.value_numeric) - min(c.value_numeric) AS variance
        FROM claim c JOIN document d ON d.id = c.document_id
        WHERE c.claim_type = 'total_borrowings'
        GROUP BY c.fact_key
        HAVING count(DISTINCT d.source_name) > 1
        """
    )
    corroborated = {r["fact_key"]: r["variance"] for r in rows
                    if not one("SELECT 1 AS x FROM conflict WHERE fact_key = %s",
                               (r["fact_key"],))}
    assert corroborated, "expected some corroborated totals"
    for fact_key, variance in corroborated.items():
        assert variance <= TOLERANCE_ABS_CR, fact_key
        assert variance is not None, f"{fact_key} lost its variance"


# --- 7. case and spacing are not disagreements -------------------------------

def test_case_differences_are_not_a_conflict(conn, sandbox):
    issuer_id = sandbox["issuer_id"]
    for source, value in (("ICRA", "Negative"), ("CARE", "NEGATIVE ")):
        doc_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, page_count)
            VALUES (%s,'rating_rationale',%s,'t','u',%s,1,'/dev/null',now(),1) RETURNING id
            """,
            (issuer_id, source, f"hash-{source}"),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_text, normalized_value, extractor, extractor_version)
            VALUES (%s,%s,'rating','rating_outlook|long_term|2025Q3','outlook',%s,%s,'f','1')
            """,
            (issuer_id, doc_id, value, normalize_value(value)),
        )
    assert reconcile(conn, issuer_id)["conflicts"] == 0


def test_normalization_keeps_the_raw_text_for_evidence(conn):
    row = one(
        """
        SELECT value_text, normalized_value FROM claim
        WHERE fact_key = 'rating_outlook|long_term|2025Q3' LIMIT 1
        """
    )
    assert row["value_text"] != row["normalized_value"] or row["value_text"].islower()
    assert row["normalized_value"] == normalize_value(row["value_text"])


@pytest.mark.parametrize("raw,expected", [
    ("Negative", "negative"),
    ("NEGATIVE", "negative"),
    (" Watch  with   Negative\nImplications ", "watch with negative implications"),
    (None, None),
])
def test_normalize_value(raw, expected):
    assert normalize_value(raw) == expected


def test_every_text_claim_has_a_normalized_value(conn):
    missing = query(
        "SELECT id FROM claim WHERE value_text IS NOT NULL AND normalized_value IS NULL")
    assert missing == []
