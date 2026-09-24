"""Rating identity and timing.

Two defects compounded in the old comparison: whichever row the extractor read
first became "the long-term issuer rating", and comparison happened inside
calendar quarters. So an agency's view of bank facilities could be reported as
disagreeing with another agency's view of debentures, and two agencies rating
five days apart across a quarter boundary never met.

The interval arithmetic is unit-tested directly; the identity rules are asserted
against the real corpus, where the same instrument appears under three
publishers' different names.
"""
from __future__ import annotations

from datetime import date

import pytest

from issuergraph.db import connect, one, query
from issuergraph.effective import _states, overlaps
from issuergraph.extractors.common import rating_identity
from issuergraph.reconcile import _merge_runs


def state(agency, cls, start, end=None, grade="AA", term="long_term"):
    return {"agency": agency, "instrument_class": cls, "term": term, "grade": grade,
            "effective_from": start, "effective_to": end}


# --- instrument identity ----------------------------------------------------

@pytest.mark.parametrize("name,expected_class", [
    ("Non-convertible debenture programme", "ncd"),          # ICRA
    ("Non Convertible Debentures", "ncd"),                   # CARE
    ("NCDs Public Issue", "ncd"),                            # Brickwork
    ("Long Term Bank Facilities", "bank_facility"),          # CARE
    ("Long-term bank lines", "bank_facility"),               # ICRA
    ("Subordinated debt programme", "subordinated_debt"),
    ("Proposed Perpetual Debt Instrument (PDI)", "perpetual_debt"),
])
def test_three_publishers_one_instrument_class(name, expected_class):
    assert rating_identity(name)[0] == expected_class


def test_bank_facilities_are_not_debentures():
    """The specific mislabelling the old comparison produced."""
    assert rating_identity("Long Term Bank Facilities")[0] != rating_identity("NCDs")[0]


@pytest.mark.parametrize("name,grade,term", [
    ("Commercial paper programme", "A1+", "short_term"),
    ("Commercial paper programme (IPO financing)", "A1+", "short_term"),
    ("Non-convertible debenture programme", "AA", "long_term"),
    # the grade settles the scale even when the instrument name does not
    ("Some unfamiliar programme", "A1+", "short_term"),
    ("Some unfamiliar programme", "AA-", "long_term"),
])
def test_rating_scale_is_identified(name, grade, term):
    assert rating_identity(name, grade)[1] == term


# --- interval arithmetic ----------------------------------------------------

def test_a_rating_stands_until_the_same_agency_acts_again():
    rows = [
        {"agency": "ICRA", "instrument_class": "ncd", "term": "long_term",
         "claim_id": 1, "action_date": date(2024, 3, 12)},
        {"agency": "ICRA", "instrument_class": "ncd", "term": "long_term",
         "claim_id": 2, "action_date": date(2025, 9, 24)},
    ]
    built = {s["claim_id"]: s for s in _states(rows)}
    assert built[1]["effective_to"] == date(2025, 9, 24)
    assert built[2]["effective_to"] is None, "the latest action is still in force"


def test_one_action_rating_several_tranches_is_one_period():
    """Three NCD tranches rated the same day share one interval, not three."""
    rows = [{"agency": "ICRA", "instrument_class": "ncd", "term": "long_term",
             "claim_id": i, "action_date": date(2025, 9, 24)} for i in range(3)]
    assert {s["effective_to"] for s in _states(rows)} == {None}


def test_another_agencys_action_does_not_close_this_one():
    rows = [
        {"agency": "ICRA", "instrument_class": "ncd", "term": "long_term",
         "claim_id": 1, "action_date": date(2024, 3, 12)},
        {"agency": "CARE", "instrument_class": "ncd", "term": "long_term",
         "claim_id": 2, "action_date": date(2025, 9, 24)},
    ]
    built = {s["claim_id"]: s for s in _states(rows)}
    assert built[1]["effective_to"] is None


def test_views_five_days_apart_overlap_across_a_quarter_boundary():
    """The case quarter buckets got exactly backwards."""
    june = state("A", "ncd", date(2025, 6, 30))
    july = state("B", "ncd", date(2025, 7, 2))
    assert overlaps(june, july) == (date(2025, 7, 2), None)


def test_a_closed_period_does_not_overlap_what_follows_it():
    earlier = state("A", "ncd", date(2024, 1, 1), date(2024, 6, 1))
    later = state("B", "ncd", date(2024, 6, 1))
    assert overlaps(earlier, later) is None, "handover day is not concurrency"


def test_overlap_is_the_intersection():
    a = state("A", "ncd", date(2024, 1, 1), date(2025, 1, 1))
    b = state("B", "ncd", date(2024, 6, 1), date(2026, 1, 1))
    assert overlaps(a, b) == (date(2024, 6, 1), date(2025, 1, 1))


# --- one disagreement stays one disagreement --------------------------------

def test_consecutive_windows_merge_into_one_run():
    """Reaffirming an unchanged difference must not mint a new conflict."""
    windows = [
        {"start": date(2025, 9, 15), "end": date(2025, 9, 24), "members": {1: "A"}},
        {"start": date(2025, 9, 24), "end": date(2026, 2, 11), "members": {2: "B"}},
        {"start": date(2026, 2, 11), "end": None, "members": {3: "A"}},
    ]
    runs = _merge_runs(windows)
    assert len(runs) == 1
    assert runs[0]["start"] == date(2025, 9, 15) and runs[0]["end"] is None
    assert set(runs[0]["members"]) == {1, 2, 3}


def test_a_runs_wording_does_not_depend_on_row_order():
    """Same-day tranches under two names: the note must not flip between them."""
    windows = [
        {"start": date(2025, 9, 24), "end": None, "members": {1: "ICRA"},
         "stated": (("Brickwork", "Stable", "NCDs Public Issue"),
                    ("ICRA", "Negative", "Non-convertible debenture programme (NCD)"))},
        {"start": date(2025, 9, 24), "end": None, "members": {2: "ICRA"},
         "stated": (("Brickwork", "Stable", "NCDs Public Issue"),
                    ("ICRA", "Negative", "NCD"))},
    ]
    forward, backward = _merge_runs(windows), _merge_runs(windows[::-1])
    assert forward[0]["stated"] == backward[0]["stated"]
    assert set(forward[0]["members"]) == set(backward[0]["members"]) == {1, 2}


def test_a_gap_starts_a_new_run():
    """They agreed for a while, then diverged again: two disagreements."""
    windows = [
        {"start": date(2024, 1, 1), "end": date(2024, 6, 1), "members": {1: "A"}},
        {"start": date(2025, 1, 1), "end": None, "members": {2: "B"}},
    ]
    assert len(_merge_runs(windows)) == 2


# --- against the real corpus ------------------------------------------------

def test_no_conflict_compares_different_instrument_classes():
    """Every rating conflict names one class, and its members rated that class."""
    for conflict in query(
            "SELECT id, fact_key FROM conflict WHERE fact_key LIKE 'rating\\_%%'"):
        instrument_class = conflict["fact_key"].split("|")[1]
        classes = query(
            """
            SELECT DISTINCT r.instrument_class FROM conflict_member m
            JOIN rating_action r ON r.claim_id = m.claim_id
            WHERE m.conflict_id = %s
            """,
            (conflict["id"],),
        )
        assert [c["instrument_class"] for c in classes] == [instrument_class], \
            conflict["fact_key"]


def test_no_conflict_compares_two_rating_scales():
    for conflict in query(
            "SELECT id, fact_key FROM conflict WHERE fact_key LIKE 'rating\\_%%'"):
        terms = query(
            """
            SELECT DISTINCT r.term FROM conflict_member m
            JOIN rating_action r ON r.claim_id = m.claim_id
            WHERE m.conflict_id = %s
            """,
            (conflict["id"],),
        )
        assert len(terms) == 1, conflict["fact_key"]


def test_no_rating_conflict_is_one_agency_disagreeing_with_itself():
    for conflict in query(
            "SELECT id, fact_key FROM conflict WHERE fact_key LIKE 'rating\\_%%'"):
        agencies = query(
            """
            SELECT DISTINCT r.agency FROM conflict_member m
            JOIN rating_action r ON r.claim_id = m.claim_id
            WHERE m.conflict_id = %s
            """,
            (conflict["id"],),
        )
        assert len(agencies) == 2, conflict["fact_key"]


def test_no_quarter_keyed_conflicts_remain():
    stale = query(
        r"SELECT fact_key FROM conflict WHERE fact_key ~ '\|\d{4}Q[1-4]$'")
    assert stale == [], f"calendar-quarter keys still in use: {stale}"


def test_the_brickwork_icra_notch_difference_is_one_conflict():
    """AA+ against AA on debentures, continuous since Brickwork's action."""
    rows = query(
        "SELECT fact_key, subject FROM conflict "
        "WHERE fact_key LIKE 'rating_grade|ncd|long_term|Brickwork~ICRA|%%'"
    )
    assert len(rows) == 1, rows
    assert rows[0]["fact_key"].endswith("2025-09-15")
    assert "still in force" in rows[0]["subject"]


def test_every_rating_state_is_either_current_or_closed_by_a_later_action():
    dangling = query(
        """
        SELECT s.id FROM rating_state s
        WHERE s.effective_to IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM rating_state n
            WHERE n.agency = s.agency AND n.instrument_class = s.instrument_class
              AND n.term = s.term AND n.effective_from = s.effective_to
        )
        """
    )
    assert dangling == [], "a closed state must be closed by a real later action"


def test_each_agency_and_class_has_exactly_one_current_state():
    rows = query(
        """
        SELECT agency, instrument_class, term, count(DISTINCT effective_from) AS n
        FROM rating_state WHERE effective_to IS NULL
        GROUP BY agency, instrument_class, term HAVING count(DISTINCT effective_from) > 1
        """
    )
    assert rows == [], f"more than one current rating for the same instrument: {rows}"


def test_rating_states_are_rebuilt_not_accumulated():
    from issuergraph.effective import build_rating_states
    before = one("SELECT count(*) AS n FROM rating_state")["n"]
    with connect() as conn:
        build_rating_states(conn, one("SELECT id FROM issuer")["id"])
        conn.commit()
    assert one("SELECT count(*) AS n FROM rating_state")["n"] == before
