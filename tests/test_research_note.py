"""Phase 9 PR C: a ResearchNote may only cite evidence its agent was actually
served, for the same ticker and as-of date, and must state the freshness it was given."""

import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from agent_fixtures import rich_state
from sqlalchemy.orm import Session

import alpha_lab.agent_interface.tools as tools_module
from alpha_lab.agent_interface import AgentContext, make_readonly_engine
from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.research_note import ResearchNote, validate_note
from alpha_lab.agent_interface.service import AgentService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 20)
PAST = "2026-09-01"


@pytest.fixture
def served(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'research.db'}"
    writer = make_engine(url)
    create_schema(writer)
    with Session(writer) as session:
        for ticker in ("TST", "OTH"):
            session.add(Security(ticker=ticker, company_name=ticker, asset_type="EQUITY", is_tracked=True))
            session.add(Price(ticker=ticker, date=TODAY, close=1, adjusted_close=1, provider="test",
                              ingested_at=datetime(2026, 9, 19, tzinfo=UTC)))
        session.commit()
    writer.dispose()

    def fake(engine, settings, ticker, evaluation_date=None, **kwargs):
        return rich_state(ticker=ticker, evaluation_date=evaluation_date or TODAY, universe_basis="snapshot")

    monkeypatch.setattr(tools_module, "get_research_state", fake)
    monkeypatch.setattr(
        tools_module, "HistoricalScoringService",
        lambda engine, settings: type("S", (), {"score_universe_as_of": lambda self, *a, **k: []})(),
    )
    engine = make_readonly_engine(url)
    log = CallLog(tmp_path / "calls.db")
    service = AgentService(AgentContext(engine, load_settings(), lambda: TODAY), log)
    yield service, log
    log.close()
    engine.dispose()


def _evidence(envelope):
    return envelope["data"]["evidence"]


def _note(summary, evidence, *, ticker="TST", as_of=None, **overrides):
    """A valid note built from what was served: one provenance id, one evidence id + its value."""
    item = next(e for e in evidence if e["value"] is not None and not isinstance(e["value"], (dict, list)))
    provenance = summary["provenance_ids"][0]
    note = {
        "ticker": ticker,
        "as_of": as_of,
        "summary": "Fundamentals and price behaviour agree; data is current.",
        "claims": [{
            "text": "A fundamentals-led positive read.",
            "citations": [{"kind": "provenance", "id": provenance}, {"kind": "evidence", "id": item["evidence_id"]}],
            "quotes": [{"evidence_id": item["evidence_id"], "value": item["value"]}],
        }],
        "freshness": {"price_data_through": summary["freshness"]["price_data_through"],
                      "price_status": summary["freshness"]["price_status"]},
        "limitations": ["Conviction is unvalidated."],
    }
    note.update(overrides)
    return note


@pytest.fixture
def session(served):
    service, log = served
    summary = service.handle("get_research_summary", {"ticker": "TST"}, client="agent")
    evidence = service.handle("list_evidence", {"ticker": "TST", "page_size": 100}, client="agent")
    assert summary["ok"] and evidence["ok"]
    return service, log, summary, _evidence(evidence)


def codes(report):
    return [issue.code for issue in report.issues]


# --- a valid note ------------------------------------------------------------------

def test_a_note_citing_only_what_was_served_with_the_served_freshness_is_valid(session):
    _, log, summary, evidence = session
    report = validate_note(_note(summary, evidence), log)
    assert report.ok and report.issues == [] and report.citations_checked == 2
    assert report.calls_in_scope == 2 and report.calls_considered == 2
    assert validate_note(json.dumps(_note(summary, evidence)), log).ok  # JSON text works too


# --- citations ----------------------------------------------------------------------

def test_an_invented_citation_is_rejected_as_never_served(session):
    _, log, summary, evidence = session
    note = _note(summary, evidence)
    note["claims"][0]["citations"].append({"kind": "evidence", "id": "analyst:made_up"})
    note["claims"][0]["citations"].append({"kind": "provenance", "id": "f" * 64})
    report = validate_note(note, log)
    assert not report.ok and codes(report) == ["never_served", "never_served"]
    assert {i.citation_id for i in report.issues} == {"analyst:made_up", "f" * 64}
    assert all(i.claim_index == 0 for i in report.issues)


def test_a_historical_note_is_checked_against_the_historical_calls(session):
    service, log, summary, evidence = session
    historical = service.handle("get_research_summary", {"ticker": "TST", "as_of": PAST}, client="agent")
    historical_evidence = service.handle("list_evidence", {"ticker": "TST", "as_of": PAST, "page_size": 100}, client="agent")
    assert historical["ok"] and historical_evidence["ok"]
    assert validate_note(_note(historical, _evidence(historical_evidence), as_of=PAST), log).ok
    assert validate_note(_note(summary, evidence), log).ok  # and the live note still stands on its own calls


def test_scope_is_proven_with_ids_that_differ_per_ticker_and_date(served):
    _, log = served
    log.record(tool="get_research_summary", arguments={"ticker": "TST"}, duration_ms=1, client="agent", envelope={
        "ok": True, "ticker": "TST", "requested_as_of": None, "system_date": "2026-09-20",
        "provenance_ids": ["tst-live"], "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"}, "data": {}})
    log.record(tool="get_research_summary", arguments={"ticker": "TST", "as_of": PAST}, duration_ms=1, client="agent", envelope={
        "ok": True, "ticker": "TST", "requested_as_of": PAST, "system_date": "2026-09-20",
        "provenance_ids": ["tst-past"], "freshness": {"price_data_through": "2026-08-31", "price_status": "fresh"}, "data": {}})
    log.record(tool="get_research_summary", arguments={"ticker": "OTH"}, duration_ms=1, client="agent", envelope={
        "ok": True, "ticker": "OTH", "requested_as_of": None, "system_date": "2026-09-20",
        "provenance_ids": ["oth-live"], "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"}, "data": {}})

    def note(ticker, as_of, cited, through):
        return {"ticker": ticker, "as_of": as_of, "summary": "s", "freshness": {"price_data_through": through, "price_status": "fresh"},
                "claims": [{"text": "c", "citations": [{"kind": "provenance", "id": cited}]}]}

    assert validate_note(note("TST", None, "tst-live", "2026-09-20"), log).ok
    assert validate_note(note("TST", PAST, "tst-past", "2026-08-31"), log).ok
    wrong_ticker = validate_note(note("TST", None, "oth-live", "2026-09-20"), log)
    assert codes(wrong_ticker) == ["served_for_other_scope"] and "OTH" in wrong_ticker.issues[0].message
    wrong_date = validate_note(note("TST", PAST, "tst-live", "2026-08-31"), log)
    assert codes(wrong_date) == ["served_for_other_scope"] and "live" in wrong_date.issues[0].message
    assert codes(validate_note(note("TST", None, "tst-past", "2026-09-20"), log)) == ["served_for_other_scope"]


def test_an_id_given_the_wrong_kind_is_rejected(session):
    _, log, summary, evidence = session
    note = _note(summary, evidence)
    served_evidence_id = note["claims"][0]["citations"][1]["id"]
    note["claims"][0]["citations"][1] = {"kind": "provenance", "id": served_evidence_id}
    assert codes(validate_note(note, log)) == ["wrong_kind"]


def test_only_the_requested_page_of_evidence_counts_as_served(served):
    service, log = served
    summary = service.handle("get_research_summary", {"ticker": "TST"}, client="agent")
    first_page = service.handle("list_evidence", {"ticker": "TST", "page_size": 1}, client="agent")
    # read the full list without serving it to the agent (not through the logged service)
    everything = tools_module.call_tool(service.context, "list_evidence", {"ticker": "TST", "page_size": 100})["data"]["evidence"]
    assert first_page["data"]["has_more"] is True and len(everything) > 1
    unseen = next(e for e in everything if e["evidence_id"] != first_page["data"]["evidence"][0]["evidence_id"])
    note = _note(summary, first_page["data"]["evidence"])
    note["claims"][0]["citations"].append({"kind": "evidence", "id": unseen["evidence_id"]})
    report = validate_note(note, log)
    assert codes(report) == ["never_served"] and report.issues[0].citation_id == unseen["evidence_id"]


def test_compare_rows_serve_each_compared_tickers_provenance(served):
    service, log = served
    compared = service.handle("compare", {"tickers": ["TST", "OTH"]}, client="agent")
    row_ids = [r["deterministic_score"]["provenance_id"] for r in compared["data"]["rows"]]
    assert all(row_ids)
    note = {"ticker": "TST", "summary": "s", "freshness": {
                "price_data_through": compared["data"]["rows"][0]["price_data_through"],
                "price_status": compared["data"]["rows"][0]["price_status"]},
            "claims": [{"text": "c", "citations": [{"kind": "provenance", "id": row_ids[0]}]}]}
    assert validate_note(note, log).ok


# --- quotes -------------------------------------------------------------------------

def test_a_quoted_figure_must_equal_the_served_value(session):
    _, log, summary, evidence = session
    note = _note(summary, evidence)
    note["claims"][0]["quotes"][0]["value"] = "definitely not the served value"
    report = validate_note(note, log)
    assert codes(report) == ["value_mismatch"] and "was served as" in report.issues[0].message
    numeric = next(e for e in evidence if isinstance(e["value"], (int, float)) and not isinstance(e["value"], bool))
    good = _note(summary, evidence)
    good["claims"][0]["quotes"] = [{"evidence_id": numeric["evidence_id"], "value": numeric["value"]}]
    good["claims"][0]["citations"].append({"kind": "evidence", "id": numeric["evidence_id"]})
    assert validate_note(good, log).ok
    good["claims"][0]["quotes"][0]["value"] = numeric["value"] + 1
    assert codes(validate_note(good, log)) == ["value_mismatch"]


def test_a_quote_of_evidence_served_only_by_id_cannot_be_checked_and_is_rejected(served):
    _, log = served
    log.record(tool="get_domain", arguments={"ticker": "TST", "domain": "ai_rating"}, duration_ms=1, client="agent", envelope={
        "ok": True, "ticker": "TST", "requested_as_of": None, "system_date": "2026-09-20", "provenance_ids": [],
        "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"},
        "data": {"value": {"supporting_evidence": ["analyst:rating"]}}})
    base = {"ticker": "TST", "summary": "s", "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"}}
    cited = {**base, "claims": [{"text": "c", "citations": [{"kind": "evidence", "id": "analyst:rating"}]}]}
    assert validate_note(cited, log).ok  # the id was served
    quoted = {**base, "claims": [{"text": "c", "citations": [{"kind": "evidence", "id": "analyst:rating"}],
                                  "quotes": [{"evidence_id": "analyst:rating", "value": "BUY"}]}]}
    assert codes(validate_note(quoted, log)) == ["value_not_served"]


# --- freshness ------------------------------------------------------------------------

def test_the_stated_freshness_must_be_the_served_freshness(session):
    _, log, summary, evidence = session
    note = _note(summary, evidence, freshness={"price_data_through": "2026-09-01", "price_status": "fresh"})
    report = validate_note(note, log)
    assert codes(report) == ["freshness_mismatch"] and "served" in report.issues[0].message
    stale = _note(summary, evidence, freshness={"price_data_through": summary["freshness"]["price_data_through"],
                                                "price_status": "stale"})
    assert codes(validate_note(stale, log)) == ["freshness_mismatch"]


def test_a_note_for_a_ticker_with_no_served_freshness_is_rejected(served):
    _, log = served
    note = {"ticker": "TST", "summary": "s", "freshness": {"price_data_through": None, "price_status": "missing"},
            "claims": [{"text": "c", "citations": [{"kind": "provenance", "id": "x"}]}]}
    assert set(codes(validate_note(note, log))) == {"never_served", "freshness_not_served"}


# --- which calls count --------------------------------------------------------------------

def test_only_successful_calls_in_the_window_from_the_named_client_serve_evidence(served):
    _, log = served
    envelope = {"ok": True, "ticker": "TST", "requested_as_of": None, "system_date": "2026-09-20",
                "provenance_ids": ["live-id"], "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"}, "data": {}}
    log.record(tool="get_research_summary", arguments={}, envelope={**envelope, "ok": False, "error": {"code": "x"}},
               duration_ms=1, client="agent", now=datetime(2026, 9, 20, 10, tzinfo=UTC))
    log.record(tool="get_research_summary", arguments={}, envelope=envelope, duration_ms=1, client="someone-else",
               now=datetime(2026, 9, 20, 11, tzinfo=UTC))
    log.record(tool="get_research_summary", arguments={}, envelope=envelope, duration_ms=1, client="agent",
               now=datetime(2026, 9, 19, 8, tzinfo=UTC))
    note = {"ticker": "TST", "summary": "s", "freshness": {"price_data_through": "2026-09-20", "price_status": "fresh"},
            "claims": [{"text": "c", "citations": [{"kind": "provenance", "id": "live-id"}]}]}
    assert not validate_note(note, log, client="agent", since=datetime(2026, 9, 20, tzinfo=UTC)).ok  # failed / other client / too early
    assert validate_note(note, log, client="someone-else").ok
    assert validate_note(note, log, client="agent", since=datetime(2026, 9, 19, tzinfo=UTC), until=datetime(2026, 9, 19, 23, tzinfo=UTC)).ok
    assert validate_note(note, log).ok


# --- the note itself -----------------------------------------------------------------------

def test_malformed_notes_are_schema_issues_not_exceptions(served):
    _, log = served
    good_claim = {"text": "c", "citations": [{"kind": "provenance", "id": "x"}]}
    base = {"ticker": "TST", "summary": "s", "freshness": {"price_data_through": None, "price_status": "missing"},
            "claims": [good_claim]}
    bad = [
        "{not json", [], {**base, "claims": []}, {**base, "extra": 1}, {**base, "ticker": "bad ticker!"},
        {**base, "claims": [{"text": "c", "citations": []}]},
        {**base, "claims": [{"text": "c", "citations": [{"kind": "other", "id": "x"}]}]},
        {**base, "freshness": {"price_data_through": None, "price_status": "great"}},
        {**base, "claims": [good_claim] * 31}, {**base, "summary": "x" * 2_000},
    ]
    for raw in bad:
        report = validate_note(raw, log)
        assert report.ok is False and codes(report) == ["schema"], raw


def test_control_and_bidirectional_characters_in_note_text_are_rejected(session):
    _, log, summary, evidence = session
    note = _note(summary, evidence)
    note["claims"][0]["text"] = "positive\u202e read"
    note["summary"] = "ok\u200b"
    report = validate_note(note, log)
    assert sorted(codes(report)) == ["unsafe_text", "unsafe_text"]


def test_the_note_model_normalises_the_ticker_and_forbids_unknown_fields():
    note = ResearchNote.model_validate({"ticker": " tst ", "summary": "s",
                                       "freshness": {"price_data_through": None, "price_status": "missing"},
                                       "claims": [{"text": "c", "citations": [{"kind": "provenance", "id": "x"}]}]})
    assert note.ticker == "TST"


# --- the log and the CLI --------------------------------------------------------------------

def test_a_read_only_log_cannot_be_written(served):
    _, log = served
    reader = CallLog(log.path, read_only=True)
    with pytest.raises(PermissionError):
        reader.record(tool="t", arguments={}, envelope={"ok": True}, duration_ms=1)
    assert len(reader.entries()) == len(log.entries())
    reader.close()
    with pytest.raises(FileNotFoundError):
        CallLog(log.path.parent / "missing.db", read_only=True)


def _cli():
    spec = importlib.util.spec_from_file_location("validate_note_script", ROOT / "scripts" / "validate_research_note.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_reports_validity_through_its_exit_code(session, tmp_path, capsys):
    _, log, summary, evidence = session
    cli = _cli()
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(_note(summary, evidence)))
    invented = _note(summary, evidence)
    invented["claims"][0]["citations"][0] = {"kind": "provenance", "id": "invented"}
    bad.write_text(json.dumps(invented))
    assert cli.main([str(good), "--log", str(log.path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main([str(bad), "--log", str(log.path)]) == 1
    assert json.loads(capsys.readouterr().out)["issues"][0]["code"] == "never_served"
    assert cli.main([str(good), "--log", str(tmp_path / "missing.db")]) == 2
    assert cli.main([str(tmp_path / "nope.json"), "--log", str(log.path)]) == 2
    assert cli.main([str(good), "--log", str(log.path), "--since", (datetime.now(UTC) + timedelta(days=1)).isoformat()]) == 1
