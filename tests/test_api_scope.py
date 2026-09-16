"""Issuer scoping and conflict resolution status in the read layer.

Both are failures of omission, so both need a test that creates the condition
the single-issuer corpus can never produce: a second issuer, and a conflict
that has stopped recurring. Each fixture writes, asserts, and deletes — the
issuer row cascades, so cleanup is one DELETE.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from issuergraph import api
from issuergraph.db import connect, one, query


@pytest.fixture
def second_issuer():
    """A second issuer with one document and one claim of every shape.

    Deliberately reuses the first issuer's fact_keys and source_name: if an
    endpoint is unscoped, the rows blend instead of merely doubling, which is
    the failure that matters.
    """
    with connect() as conn:
        base = conn.execute("SELECT id FROM issuer ORDER BY id LIMIT 1").fetchone()["id"]
        template = conn.execute(
            "SELECT * FROM document WHERE issuer_id = %s ORDER BY id LIMIT 1", (base,)
        ).fetchone()

        issuer_id = conn.execute(
            "INSERT INTO issuer (name, cin) VALUES ('ZZ Test Issuer', 'TESTCIN') "
            "RETURNING id"
        ).fetchone()["id"]
        document_id = conn.execute(
            """
            INSERT INTO document (issuer_id, doc_type, source_name, title, url, sha256,
                                  byte_size, local_path, retrieved_at, published_date,
                                  page_count)
            VALUES (%s, 'rating_rationale', %s, 'ZZ Test Rationale', 'https://example.test',
                    'zz-test-sha', 1, %s, now(), DATE '2026-01-01', 1)
            RETURNING id
            """,
            (issuer_id, template["source_name"], template["local_path"]),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO document_page (document_id, page_no, text, width, height, word_map)
            VALUES (%s, 1, 'ZZ', 1, 1, '[]'::jsonb)
            """,
            (document_id,),
        )
        claim_id = conn.execute(
            """
            INSERT INTO claim (issuer_id, document_id, claim_type, fact_key, subject,
                               value_numeric, value_unit, basis, as_of_date,
                               extractor, extractor_version)
            VALUES (%s, %s, 'total_borrowings', 'total_borrowings|consolidated|2024-03-31',
                    'ZZ total borrowings', 999999, 'INR_CRORE', 'consolidated',
                    DATE '2024-03-31', 'zz', '0')
            RETURNING id
            """,
            (issuer_id, document_id),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO rationale_diff (issuer_id, agency, to_document_id, to_date,
                                        section, direction, to_text)
            VALUES (%s, 'ZZ', %s, DATE '2026-01-01', 'strengths', 'added', 'ZZ change')
            """,
            (issuer_id, document_id),
        )
        conn.commit()

    yield {"issuer_id": issuer_id, "claim_id": claim_id, "base_id": base}

    with connect() as conn:
        conn.execute("DELETE FROM issuer WHERE id = %s", (issuer_id,))
        conn.commit()


# --- issuer scoping ---------------------------------------------------------

def test_single_issuer_needs_no_parameter():
    """The slice must keep working unparameterised while one issuer is loaded."""
    assert api.resolve_issuer(None) == one("SELECT id FROM issuer")["id"]


def test_two_issuers_refuse_to_guess(second_issuer):
    with pytest.raises(HTTPException) as exc:
        api.resolve_issuer(None)
    assert exc.value.status_code == 400
    assert "issuer_id" in exc.value.detail


def test_unknown_issuer_is_404():
    with pytest.raises(HTTPException) as exc:
        api.resolve_issuer(10**9)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("endpoint", ["debt", "ratings", "conflicts",
                                      "corroborations", "changes", "issuer"])
def test_every_fact_endpoint_is_scoped(second_issuer, endpoint):
    """Not just "returns something" — the other issuer's rows must be absent."""
    payload = getattr(api, endpoint)(issuer_id=second_issuer["issuer_id"])
    rows = payload if isinstance(payload, list) else [payload]
    blob = repr(rows)
    assert "IIFL" not in blob.upper() or endpoint == "issuer"

    ours = getattr(api, endpoint)(issuer_id=second_issuer["base_id"])
    assert "ZZ Test" not in repr(ours), f"{endpoint} leaked the second issuer"


def test_second_issuer_sees_only_its_own_debt(second_issuer):
    totals = api.debt(issuer_id=second_issuer["issuer_id"])["totals"]
    assert [row["claim_id"] for row in totals] == [second_issuer["claim_id"]]


def test_second_issuer_sees_only_its_own_changes(second_issuer):
    rows = api.changes(issuer_id=second_issuer["issuer_id"])
    assert len(rows) == 1 and rows[0]["agency"] == "ZZ"


def test_issuers_lists_both(second_issuer):
    names = {row["name"] for row in api.issuers()}
    assert "ZZ Test Issuer" in names and len(names) >= 2


# --- conflict resolution status --------------------------------------------

@pytest.fixture
def resolved_conflict():
    """Stamp one open conflict resolved, then put it back."""
    row = one("SELECT id FROM conflict WHERE resolved_at IS NULL ORDER BY id LIMIT 1")
    if not row:
        pytest.skip("no open conflict to resolve — run the pipeline first")
    with connect() as conn:
        conn.execute("UPDATE conflict SET resolved_at = now() WHERE id = %s", (row["id"],))
        conn.commit()
    yield row["id"]
    with connect() as conn:
        conn.execute("UPDATE conflict SET resolved_at = NULL WHERE id = %s", (row["id"],))
        conn.commit()


def test_resolved_conflicts_are_hidden_by_default(resolved_conflict):
    assert resolved_conflict not in {c["id"] for c in api.conflicts()}


def test_resolved_conflicts_are_available_on_request(resolved_conflict):
    rows = {c["id"]: c for c in api.conflicts(include_resolved=True)}
    assert rows[resolved_conflict]["status"] == "resolved"
    assert rows[resolved_conflict]["resolved_at"] is not None


def test_open_conflicts_carry_their_history():
    for conflict in api.conflicts():
        assert conflict["status"] == "open"
        assert conflict["resolved_at"] is None
        assert conflict["first_detected_at"] is not None
        assert conflict["last_seen_at"] is not None


def test_resolved_conflict_keeps_its_members(resolved_conflict):
    """Resolution is a status change, not a deletion: the evidence stays."""
    rows = {c["id"]: c for c in api.conflicts(include_resolved=True)}
    assert len(rows[resolved_conflict]["members"]) >= 2
