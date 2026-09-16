"""The pilot request flow.

The property worth protecting: a visitor is told "stored" only when a row
exists. Everything else here — validation, duplicate handling, throttling — is
in service of that, plus the promise on the page that a submission is used to
reply and nothing else.
"""
from __future__ import annotations

import pytest

import asgi
from issuergraph import pilot
from issuergraph.db import connect, one, query

VALID = {
    "name": "Test Analyst",
    "email": "pytest-analyst@examplecredit.test",
    "organisation": "Example Credit Partners",
    "role": "Credit analyst",
    "issuers": "IIFL Finance, Muthoot Finance, Shriram Finance",
    "workflow": "Quarterly surveillance, reconciling agency rationales against filings.",
    "source": "pytest",
}


@pytest.fixture(autouse=True)
def clean_slate():
    """Each test starts with no rows and no throttling history of its own."""
    def purge():
        with connect() as conn:
            conn.execute("DELETE FROM pilot_request WHERE email_key LIKE %s",
                         ("%examplecredit.test",))
            conn.execute("DELETE FROM pilot_request WHERE email_key LIKE %s", ("%.test",))
            conn.execute("DELETE FROM pilot_submission_log")
            conn.commit()
    purge()
    yield
    purge()


def submit(**overrides):
    return asgi.post("/api/pilot", json={**VALID, **overrides})


def mine(column: str = "count(*) AS n"):
    """Rows this test created. The suite must not depend on an empty table."""
    return query(f"SELECT {column} FROM pilot_request WHERE email_key LIKE '%%.test'")


def my_count() -> int:
    return mine()[0]["n"]


# --- validation -------------------------------------------------------------

def test_an_empty_form_is_rejected_field_by_field():
    response = asgi.post("/api/pilot", json={})
    assert response.status == 422
    fields = response.json()["fields"]
    assert set(fields) >= {"name", "email", "organisation", "role", "issuers", "workflow"}
    assert all(message.endswith(".") for message in fields.values()), fields


@pytest.mark.parametrize("email", ["nope", "a@b", "a b@c.com", "@example.com", ""])
def test_malformed_addresses_are_rejected(email):
    response = submit(email=email)
    assert response.status == 422
    assert "email" in response.json()["fields"]


def test_nothing_is_stored_when_validation_fails():
    submit(email="nope")
    assert my_count() == 0


def test_notes_are_optional():
    assert submit(notes="").status == 200


def test_a_one_word_workflow_is_not_enough_to_scope_anything():
    assert submit(workflow="stuff").status == 422


def test_whitespace_is_tidied_rather_than_stored_raw():
    submit(name="  Test   Analyst  ", issuers="IIFL\n\nMuthoot")
    row = mine("name, issuers")[0]
    assert row["name"] == "Test Analyst"
    assert row["issuers"] == "IIFL Muthoot"


# --- storage ----------------------------------------------------------------

def test_success_means_the_row_exists():
    response = submit()
    assert response.status == 200 and response.json()["stored"] is True
    row = one("SELECT * FROM pilot_request WHERE email_key = %s", (VALID["email"],))
    assert row and row["organisation"] == VALID["organisation"]
    assert row["workflow"] == VALID["workflow"]


def test_the_submitted_source_is_recorded():
    submit(source="demo")
    assert mine("source")[0]["source"] == "demo"


def test_a_resubmission_updates_one_lead_rather_than_making_two():
    submit()
    second = submit(organisation="Example Credit Partners LLP", role="Head of research")
    assert second.json()["duplicate"] is True
    rows = mine("organisation, role, submissions")
    assert len(rows) == 1
    assert rows[0]["organisation"] == "Example Credit Partners LLP"
    assert rows[0]["submissions"] == 2


def test_duplicate_detection_ignores_case_and_spacing():
    submit()
    submit(email=f"  {VALID['email'].upper()} ")
    assert my_count() == 1


def test_a_free_address_is_accepted_but_noted():
    """We ask for a work address; we do not refuse anyone over it."""
    response = submit(email="someone@gmail.com")
    assert response.status == 200
    assert response.json()["work_email"] is False


# --- failure is reported, never disguised -----------------------------------

def test_storage_failure_returns_503_and_a_way_to_reach_us(monkeypatch):
    def unavailable(*args, **kwargs):
        raise pilot.StorageUnavailable("connection refused")
    monkeypatch.setattr(pilot, "store", unavailable)

    response = submit()
    assert response.status == 503
    body = response.json()
    assert body.get("stored") is not True
    assert "@" in body["fallback_email"]
    assert my_count() == 0


def test_a_bad_dsn_raises_rather_than_reporting_success(monkeypatch):
    monkeypatch.setenv("ISSUERGRAPH_DSN", "postgresql:///issuergraph_does_not_exist")
    request = pilot.parse(VALID)
    with pytest.raises(pilot.StorageUnavailable):
        pilot.store(request)


def test_non_json_bodies_are_refused():
    response = asgi.request("POST", "/api/pilot", headers={"content-type": "text/plain"})
    assert response.status == 400


# --- throttling -------------------------------------------------------------

def test_repeated_submissions_are_throttled(monkeypatch):
    monkeypatch.setattr(pilot, "rate_limit", lambda: (3, 3600))
    for attempt in range(3):
        assert submit(email=f"rate{attempt}@examplecredit.test").status == 200
    blocked = submit(email="rate-blocked@examplecredit.test")
    assert blocked.status == 429
    assert "Too many requests" in blocked.json()["error"]


def test_throttling_stores_a_hash_not_an_address(monkeypatch):
    monkeypatch.setattr(pilot, "rate_limit", lambda: (5, 3600))
    submit()
    logged = query("SELECT client_hash FROM pilot_submission_log")
    assert logged, "nothing logged"
    for row in logged:
        assert len(row["client_hash"]) == 64 and "127.0.0.1" not in row["client_hash"]


def test_the_hash_is_salted_so_it_is_not_a_bare_address_digest(monkeypatch):
    import hashlib
    bare = hashlib.sha256(b"203.0.113.9").hexdigest()
    assert pilot.client_hash("203.0.113.9") != bare


# --- the promise on the page ------------------------------------------------

def test_the_schema_cannot_hold_marketing_state():
    columns = {row["column_name"] for row in query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'pilot_request'")}
    for unwanted in ("consent", "marketing_opt_in", "campaign", "utm_source",
                     "ip_address", "user_agent"):
        assert unwanted not in columns, f"pilot_request grew a {unwanted} column"


def test_crm_forwarding_is_off_unless_configured():
    request = pilot.parse(VALID)
    assert pilot.forward_to_crm(request, 1) is False
