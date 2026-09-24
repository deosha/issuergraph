"""Rating-history annexures, corpus gaps, and what they do to in-force periods.

The CARE defect generalised: a rating was treated as in force until the same
agency's next *ingested* action. Each agency's rationale lists its own past
actions; one it lists that we hold no rationale for is a missing document.
It becomes a corpus gap, ends the preceding state early, opens a state of its
own (provenance history_annexure), and flags any disagreement computed across
it — never suppressing it.

The first block is a fixture with no database: the rule itself. The rest reads
the IIFL corpus.
"""
from __future__ import annotations

from datetime import date

import pytest

from issuergraph import api
from issuergraph.completeness import find_gaps, matches
from issuergraph.db import one, query
from issuergraph.effective import _states
from issuergraph.extractors import care, history, icra
from issuergraph.models import ExtractedClaim, HistoryDetail


# --- the rule, on a fixture --------------------------------------------------

def primary(claim_id, day, grade="AA", action=None, agency="X", cls="ncd"):
    return {"claim_id": claim_id, "agency": agency, "instrument": f"{cls} tranche",
            "instrument_class": cls, "term": "long_term", "grade": grade,
            "outlook": None, "watch": None, "action": action, "action_date": day}


def listed(claim_id, day, grade="AA", agency="X", cls="ncd", withdrawn=False):
    return {"claim_id": claim_id, "agency": agency, "instrument": f"{cls} (history)",
            "instrument_class": cls, "term": "long_term", "action_date": day,
            "grade": grade, "outlook": "Stable", "watch": None, "withdrawn": withdrawn}


def as_action(gap: dict, claim_id: int) -> dict:
    return {"claim_id": claim_id, "agency": gap["agency"], "instrument": "history",
            "instrument_class": gap["instrument_class"], "term": gap["term"],
            "grade": gap["grade"] or None, "outlook": gap["outlook"],
            "watch": gap["watch"], "action": None, "action_date": gap["action_date"]}


def test_a_listed_action_we_lack_is_a_gap_and_truncates_the_earlier_period():
    """History lists 1 Jun; we hold 1 Jan and 1 Dec. 1 Jan ends on 1 Jun."""
    held = [primary(1, date(2024, 1, 1)), primary(2, date(2024, 12, 1))]
    annexure = [listed(10, date(2024, 1, 2)),                   # 1 Jan, a day off
                listed(11, date(2024, 6, 1), grade="AA-")]      # not held

    gaps = find_gaps(annexure, held)
    assert [(g["action_date"], g["grade"], g["scope"], g["evidence"]) for g in gaps] == [
        (date(2024, 6, 1), "AA-", "within_corpus", [11])]

    states = {s["claim_id"]: s for s in _states(held, [as_action(gaps[0], 11)])}
    january = states[1]
    assert january["effective_to"] == date(2024, 6, 1)
    assert january["end_basis"] == "history"
    assert january["superseded_by_claim_id"] == 11
    june = states[11]
    assert (june["provenance"], june["grade"]) == ("history_annexure", "AA-")
    assert (june["effective_from"], june["effective_to"], june["end_basis"]) == (
        date(2024, 6, 1), date(2024, 12, 1), "primary")
    assert states[2]["effective_to"] is None and states[2]["end_basis"] is None


def test_without_the_gap_the_earlier_period_runs_on():
    """The defect the gap corrects: 1 Jan standing until 1 Dec."""
    held = [primary(1, date(2024, 1, 1)), primary(2, date(2024, 12, 1))]
    january = next(s for s in _states(held) if s["claim_id"] == 1)
    assert (january["effective_to"], january["end_basis"]) == (date(2024, 12, 1), "primary")


def test_matching_is_three_days_and_the_same_grade():
    held = primary(1, date(2024, 3, 12))
    assert matches(listed(9, date(2024, 3, 15)), held)
    assert not matches(listed(9, date(2024, 3, 16)), held)
    assert not matches(listed(9, date(2024, 3, 12), grade="AA-"), held)
    assert not matches(listed(9, date(2024, 3, 12), cls="bank_facility"), held)
    assert not matches(listed(9, date(2024, 3, 12), agency="Y"), held)


def test_a_gradeless_withdrawal_matches_a_withdrawal():
    held = primary(1, date(2024, 9, 19), grade=None, action="withdrawn")
    assert matches(listed(9, date(2024, 9, 19), grade=None, withdrawn=True), held)
    assert not matches(listed(9, date(2024, 9, 19), grade=None, withdrawn=True),
                       primary(1, date(2024, 9, 19), action="reaffirmed"))


def test_an_action_before_everything_we_hold_truncates_nothing():
    held = [primary(1, date(2024, 1, 1))]
    gaps = find_gaps([listed(10, date(2023, 6, 1))], held)
    assert [g["scope"] for g in gaps] == ["before_corpus"]


def test_one_missing_action_listed_twice_is_one_gap_with_both_as_evidence():
    held = [primary(1, date(2024, 1, 1))]
    gaps = find_gaps([listed(10, date(2024, 6, 1)), listed(20, date(2024, 6, 1))], held)
    assert len(gaps) == 1 and gaps[0]["evidence"] == [10, 20]


def test_a_history_entry_is_never_a_primary_action():
    kwargs = dict(claim_type="rating_history", fact_key="rating_history|X", subject="s",
                  value_text="AA", extractor="e", extractor_version="1",
                  anchors=[{"page_no": 1, "char_start": 0, "char_end": 2,
                            "evidence_text": "AA"}])
    detail = HistoryDetail(agency="X", instrument="NCD", action_date=date(2024, 1, 1),
                           grade="AA")
    assert ExtractedClaim(**kwargs, history=detail).provenance == "history_annexure"
    with pytest.raises(ValueError):
        ExtractedClaim(**kwargs)                                  # no detail
    with pytest.raises(ValueError):
        ExtractedClaim(**kwargs, history=detail, provenance="primary_rationale")
    with pytest.raises(ValueError):
        HistoryDetail(agency="X", instrument="NCD", action_date=date(2024, 1, 1), grade=None)


# --- the annexure layouts ----------------------------------------------------

def pages_of(url_fragment: str) -> list[dict]:
    rows = query(
        """
        SELECT p.page_no, p.text, p.word_map FROM document_page p
        JOIN document d ON d.id = p.document_id WHERE d.url LIKE %s ORDER BY p.page_no
        """, (f"%{url_fragment}%",))
    if not rows:
        pytest.skip(f"{url_fragment} not ingested")
    return [dict(r) for r in rows]


def entries(pages, head, ends, furniture=None):
    header, body = history.table_lines(pages, head, ends, furniture)
    found, unreadable = history.table_entries(header, body)
    assert unreadable == []
    return found


def test_icra_2024_cells_split_across_a_page_break_are_merged_by_column():
    """Row 8's cells read "PP-MLD[ICRA]" on page 4 and "AA (Stable)" on page 5."""
    found = entries(pages_of("id=126125"), icra.HISTORY_HEAD, icra.HISTORY_END,
                    icra.PAGE_FURNITURE)
    split = [e for e in found if e["row"]["name"].startswith("Long-term principal protected "
                                                            "equity")
             and e["date"] == date(2022, 8, 3)]
    assert len(split) == 1
    entry = split[0]
    assert (entry["grade"], entry["outlook"]) == ("AA", "Stable")
    assert {line["page_no"] for line in entry["rating_lines"]} == {4, 5}


def test_icra_2026_centred_names_still_delimit_rows():
    found = entries(pages_of("id=140856"), icra.HISTORY_HEAD, icra.HISTORY_END,
                    icra.PAGE_FURNITURE)
    assert {e["row"]["name"] for e in found} == {
        "NCD programme", "CP", "Subordinated bonds/Debt"}


def test_the_documents_own_action_is_not_history():
    pages = pages_of("id=137962")
    claims = icra.extract(icra_meta(date(2025, 9, 24)), pages)
    dates = {c.history.action_date for c in claims if c.history}
    assert date(2025, 9, 24) not in dates and date(2024, 9, 25) in dates


def icra_meta(published):
    class Meta:
        published_date = published
    return Meta()


def test_care_entry_continued_on_the_next_page_stays_with_its_row():
    """April 2024: Subordinated Debt's 28-Sep-23 entry is printed on page 5,
    behind the page furniture and a repeated header."""
    claims = care._history(None, pages_of("202404130415"))[0]
    sub = sorted(c.history.action_date for c in claims
                 if c.history.instrument_class == "subordinated_debt")
    assert date(2023, 9, 28) in sub


def test_brickwork_spaced_modifier_and_removed_watch():
    """"BWR AA + / Stable (Reaffirmed and removed Rating Watch ...)" is AA+,
    Stable, and not on watch."""
    found = entries(pages_of("IIFL-Finance-15Sep2025"), "Rating History for the past 3 years",
                    ("\nTotal\n", "^Public Issue", "ANNEXURE I"))
    sept = [e for e in found if e["date"] == date(2024, 9, 30)]
    assert sept and all((e["grade"], e["outlook"], e["watch"]) == ("AA+", "Stable", None)
                        for e in sept)
    march = [e for e in found if e["date"] == date(2024, 3, 13)]
    assert march and all(e["watch"] == "Watch with Negative Implications" for e in march)


def test_every_history_claim_is_anchored_and_labelled_history():
    rows = query(
        """
        SELECT c.provenance, count(a.id) AS anchors
        FROM claim c LEFT JOIN evidence_anchor a ON a.claim_id = c.id
        WHERE c.claim_type = 'rating_history' GROUP BY c.id, c.provenance
        """)
    if not rows:
        pytest.skip("corpus not loaded")
    assert {r["provenance"] for r in rows} == {"history_annexure"}
    assert min(r["anchors"] for r in rows) >= 2          # the row's name and its cell


# --- the fifth layout: ICRA 25 Sep 2024 --------------------------------------

def test_a_row_whose_name_is_printed_on_the_next_page_keeps_its_entries():
    """The NCD row's type, amount and first entries sit at the foot of page 6,
    its name at the top of page 7. Rows start at the Type cell, so those
    entries are not credited to the bank-lines row above them."""
    claims = icra.extract(icra_meta(date(2024, 9, 25)), pages_of("id=130090"))
    straddling = [c for c in claims if c.history and
                  {a.page_no for a in c.anchors} == {6, 7}]
    assert straddling and {c.history.instrument_class for c in straddling} == {"ncd"}
    bank = {c.history.action_date for c in claims
            if c.history and c.history.instrument_class == "bank_facility"}
    assert bank == {date(2021, 10, 6), date(2022, 8, 3), date(2023, 8, 1),
                    date(2023, 12, 29), date(2024, 3, 12)}


def test_a_grade_broken_across_lines_is_one_grade():
    """"MLD[ICRA]A" / "A (Stable)" is AA, not A."""
    assert history.read_rating("PP- MLD[ICRA]A A (Stable)")["grade"] == "AA"
    assert history.read_rating("[ICRA]A (Stable)")["grade"] == "A"
    assert history.read_rating("[ICRA]A1+")["grade"] == "A1+"
    claims = icra.extract(icra_meta(date(2024, 9, 25)), pages_of("id=130090"))
    assert {c.history.grade for c in claims
            if c.history and c.history.instrument_class == "mld"} == {"AA"}


def test_the_table_ends_where_it_ends_on_the_page_not_in_text_order():
    """Page 7 emits the complexity table first in text order, though it is
    printed below the history table's last rows."""
    claims = icra.extract(icra_meta(date(2024, 9, 25)), pages_of("id=130090"))
    page7 = {c.history.instrument for c in claims
             if c.history and c.anchors[0].page_no == 7}
    assert {"Subordinated debt programme", "Commercial paper programme"} <= page7


def test_removed_from_watch_is_not_a_watch():
    rows = query("""SELECT DISTINCT watch, outlook FROM rating_action
                    WHERE agency = 'ICRA' AND action_date = '2024-09-25'
                      AND term = 'long_term'""")
    if not rows:
        pytest.skip("ICRA 25 Sep 2024 not loaded")
    assert rows == [{"watch": None, "outlook": "Stable"}]


def test_care_history_reads_the_outlook_after_a_semicolon():
    assert history.read_rating("1)CARE AA; Stable (28-Sep-23)")["outlook"] == "Stable"
    outlooks = {r["outlook"] for r in query(
        "SELECT outlook FROM rating_history_entry WHERE agency = 'CARE' AND watch IS NULL "
        "AND grade IS NOT NULL")}
    assert outlooks and None not in outlooks


# --- IIFL ---------------------------------------------------------------------

def test_loading_icras_september_2024_rationale_resolved_its_gap_and_kept_it():
    """The one missing document was found, loaded, and its gaps resolved —
    stamped, not deleted, with the annexure rows that listed them."""
    gaps = query(
        """
        SELECT g.instrument_class, g.resolved_at, g.first_detected_at < g.resolved_at AS ordered,
               count(e.claim_id) AS evidence
        FROM corpus_gap g JOIN corpus_gap_evidence e ON e.gap_id = g.id
        WHERE g.agency = 'ICRA' AND g.action_date = '2024-09-25'
        GROUP BY g.id ORDER BY g.instrument_class
        """)
    if not gaps:
        pytest.skip("corpus not loaded")
    assert [g["instrument_class"] for g in gaps] == [
        "bank_facility", "commercial_paper", "mld", "ncd", "subordinated_debt"]
    assert all(g["resolved_at"] and g["ordered"] and g["evidence"] for g in gaps)
    assert query("SELECT 1 FROM corpus_gap WHERE agency = 'ICRA' AND scope = 'within_corpus' "
                 "AND resolved_at IS NULL") == []


def test_icras_march_2024_watch_now_ends_on_the_primary_action():
    states = query(
        """
        SELECT effective_to, end_basis FROM rating_state
        WHERE agency = 'ICRA' AND instrument_class = 'ncd' AND effective_from = '2024-03-12'
        """)
    assert states and all((s["effective_to"], s["end_basis"]) == (date(2024, 9, 25), "primary")
                          for s in states)
    assert query("SELECT 1 FROM rating_state WHERE agency = 'ICRA' "
                 "AND provenance = 'history_annexure'") == []


def test_flags_follow_the_gaps_that_remain():
    """ICRA's gap is closed, so nothing involving ICRA alone is flagged. Crisil's
    rationales of 30 Sep 2024, 27 Jan and 24 Mar 2026 list eight actions in
    between that we do not hold; conflicts computed across them are flagged —
    except sub-debt, where Crisil's own sub-debt rating was withdrawn in 2023
    and its view comes from the annexure's subordinated NCDs (no gap there)."""
    flagged = {r["fact_key"] for r in query(
        "SELECT fact_key FROM conflict WHERE incomplete_corpus AND resolved_at IS NULL")}
    assert flagged == {
        "rating_grade|ncd|long_term|Brickwork~CRISIL|2025-09-15",
        "rating_grade|perpetual_debt|long_term|Brickwork~CRISIL|2025-09-15",
        "rating_outlook|bank_facility|long_term|CRISIL~ICRA|2025-09-24",
        "rating_outlook|ncd|long_term|CRISIL~ICRA|2025-09-24",
    }
    assert {r["agency"] for r in query(
        "SELECT DISTINCT agency FROM corpus_gap WHERE scope = 'within_corpus' "
        "AND resolved_at IS NULL")} == {"CRISIL"}


def test_the_sources_summary_counts_listed_and_loaded_actions():
    agencies = {a["agency"]: a for a in api.corpus_gaps()["agencies"]}
    assert (agencies["ICRA"]["loaded"], agencies["ICRA"]["missing_within"]) == (3, 0)
    # CARE's 20 Sep 2024 withdrawal lists its 19 Sep 2024 action, which we hold
    assert (agencies["CARE"]["loaded"], agencies["CARE"]["missing_within"]) == (3, 0)
    assert agencies["Brickwork"]["missing_within"] == 0


# --- the demo gate ------------------------------------------------------------

def test_the_snapshot_build_refuses_a_flagged_conflict_without_writing(monkeypatch):
    """Given a flagged conflict, the export stops before touching any file.
    The flag is injected, so this test can never export for real."""
    from scripts import export_demo

    real = export_demo.api.conflicts
    monkeypatch.setattr(export_demo.api, "conflicts", lambda **kw: [
        *real(**kw), {"id": 0, "fact_key": "rating_grade|ncd|long_term|X~Y|2024-01-01",
                      "subject": "injected", "incomplete_corpus": True}])
    snapshot = export_demo.OUT / "snapshot.json"
    pages = sorted(export_demo.PAGES.glob("*")) if export_demo.PAGES.exists() else []
    before = snapshot.read_bytes() if snapshot.exists() else None
    with pytest.raises(SystemExit) as refused:
        export_demo.main([])
    assert refused.value.code == 2
    assert (snapshot.read_bytes() if snapshot.exists() else None) == before
    assert (sorted(export_demo.PAGES.glob("*")) if export_demo.PAGES.exists() else []) == pages


def test_allow_gaps_is_the_only_way_past_the_gate():
    from scripts.export_demo import gap_gate

    flagged = [{"id": 1, "incomplete_corpus": True}, {"id": 2, "incomplete_corpus": False}]
    assert [c["id"] for c in gap_gate(flagged, allow_gaps=False)] == [1]
    assert gap_gate(flagged, allow_gaps=True) == []
