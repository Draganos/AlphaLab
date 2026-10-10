"""Conviction audit trail (roadmap Phase 8, PR B): an immutable, content-hashed
record of what the layer said, never read back into live research."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conviction_fixtures import COVERAGE, STALE_DAYS, _alignment, _research, _state
from sqlalchemy import event, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.conviction import (
    assess_conviction,
    assessment_history,
    content_hash,
    input_fingerprint,
    record_assessment,
)
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import ConvictionAssessmentRecord, Security
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research.analyst_consensus import AnalystRating
from alpha_lab.research.analyst_research import RevisionDirection
from alpha_lab.research_state import get_research_state


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    return engine


def _record(engine, state):
    assessment = assess_conviction(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    return assessment, record_assessment(engine, state, assessment, coverage=COVERAGE, stale_price_days=STALE_DAYS)


def test_recording_stores_the_assessment_with_hashes_and_provenance():
    engine = _engine()
    state = _state(_research(), alignment=_alignment(MacroRegime.RISK_OFF))
    assessment, inserted = _record(engine, state)
    assert inserted is True
    [row] = assessment_history(engine, "TST")
    assert (row.agreement, row.direction) == ("CONVERGENT", "POSITIVE")
    assert row.methodology_version == assessment.methodology_version
    assert row.content_hash == content_hash(assessment) and len(row.content_hash) == 64
    assert row.input_fingerprint == input_fingerprint(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    assert row.assessment == assessment.model_dump(mode="json")
    assert len(row.conflicts) == len(assessment.conflicts) > 0
    assert set(row.provenance_ids) >= {"prov-fundamentals", "prov-technicals", "prov-donatien_alignment"}
    assert row.provenance_ids == sorted(set(row.provenance_ids))


def test_an_identical_assessment_is_recorded_once_and_a_changed_one_is_appended():
    engine = _engine()
    state = _state(_research())
    assert _record(engine, state)[1] is True
    assert _record(engine, state)[1] is False  # same as the latest row -> no-op
    assert len(assessment_history(engine, "TST")) == 1
    changed = _state(_research(consensus=AnalystRating.SELL, revisions=RevisionDirection.DETERIORATING))
    assert _record(engine, changed)[1] is True  # same ticker/date, different content
    assert len(assessment_history(engine, "TST")) == 2


def test_a_return_to_an_earlier_result_on_the_same_date_is_recorded():
    """Review finding: deduping against ANY earlier row hid A -> B -> A, so the
    history said B while the layer's current output was A."""
    engine = _engine()
    first = _state(_research())
    second = _state(_research(consensus=AnalystRating.SELL, revisions=RevisionDirection.DETERIORATING))
    assert _record(engine, first)[1] is True
    assert _record(engine, second)[1] is True
    assert _record(engine, first)[1] is True  # back to the first content
    assert _record(engine, first)[1] is False  # ... and now it is the latest, so a repeat is a no-op
    history = assessment_history(engine, "TST")
    assert [row.agreement for row in history] == ["CONVERGENT", "CONTESTED", "CONVERGENT"]
    assert history[0].content_hash == history[2].content_hash != history[1].content_hash


def test_the_fingerprint_tracks_the_evidence_and_the_thresholds():
    state = _state(_research())
    base = input_fingerprint(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    assert base == input_fingerprint(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    assert base != input_fingerprint(state, coverage=COVERAGE, stale_price_days=STALE_DAYS + 1)
    later = state.model_copy(update={"evaluation_date": state.evaluation_date + timedelta(days=30)})
    assert base != input_fingerprint(later, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    refreshed = state.model_copy(update={"technicals": state.technicals.model_copy(update={"provenance_id": "new"})})
    assert base != input_fingerprint(refreshed, coverage=COVERAGE, stale_price_days=STALE_DAYS)


def test_the_audit_table_is_append_only_at_the_database_level():
    engine = _engine()
    _record(engine, _state(_research()))
    with engine.begin() as connection, pytest.raises(DatabaseError, match="append-only"):
        connection.execute(text("UPDATE conviction_assessments SET agreement = 'CONTESTED'"))
    with engine.begin() as connection, pytest.raises(DatabaseError, match="append-only"):
        connection.execute(text("DELETE FROM conviction_assessments"))
    with Session(engine) as session:  # the ORM path is blocked too
        row = session.query(ConvictionAssessmentRecord).one()
        row.agreement = "CONTESTED"
        with pytest.raises(DatabaseError, match="append-only"):
            session.commit()
    [still] = assessment_history(engine, "TST")
    assert still.agreement == "CONVERGENT"
    create_schema(engine)  # re-running the schema bootstrap keeps (not duplicates) the guards
    create_schema(engine)


def test_history_is_point_in_time_by_recording_time():
    engine = _engine()
    _record(engine, _state(_research()))
    today = datetime.now(UTC).date()
    assert len(assessment_history(engine, "TST", as_of=today)) == 1
    assert assessment_history(engine, "TST", as_of=today - timedelta(days=1)) == []
    assert assessment_history(engine, "OTHER") == []


def test_live_research_never_reads_the_audit_table():
    """Not a source of truth: assembling a state must not touch it."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="ZZZ", is_tracked=True))
        session.commit()
    _record(engine, _state(_research()))
    touched = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: touched.append(stmt))
    state = get_research_state(engine, load_settings(), "ZZZ")
    assert state.conviction is not None
    assert not [stmt for stmt in touched if "conviction_assessments" in stmt]


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "assess_conviction.py"
    spec = importlib.util.spec_from_file_location("assess_conviction_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_records_the_tracked_universe_idempotently_and_dry_run_writes_nothing():
    script = _load_script()
    engine = _engine()
    with Session(engine) as session:
        session.add_all([Security(ticker="AAA", is_tracked=True), Security(ticker="BBB", is_tracked=True)])
        session.commit()
    settings = load_settings()
    dry = script.assess_universe(engine, settings, ["AAA", "BBB"], record=False)
    assert [row["recorded"] for row in dry] == [False, False] and assessment_history(engine, "AAA") == []
    first = script.assess_universe(engine, settings, ["AAA", "bbb"], record=True)
    assert [row["ticker"] for row in first] == ["AAA", "BBB"] and all(row["recorded"] for row in first)
    second = script.assess_universe(engine, settings, ["AAA", "BBB"], record=True)
    assert not any(row["recorded"] for row in second)
    assert len(assessment_history(engine, "AAA")) == 1
    assert first[0]["assessment"].agreement.value == "INSUFFICIENT"  # no evidence is recorded honestly, not skipped


def test_company_research_page_renders_the_conviction_section(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from alpha_lab.screener import MarketScreenerService

    db_path = tmp_path / "page.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="ZZZ", company_name="Zed Corp", is_tracked=True))
        session.commit()
    MarketScreenerService(engine, load_settings()).rebuild_current_research()
    engine.dispose()
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")

    at = AppTest.from_file("app/dashboard/pages/4_Company_Research.py")
    at.run(timeout=120)

    assert not at.exception
    assert "Conviction & Conflicts" in [subheader.value for subheader in at.subheader]
    # no evidence at all: the layer says so plainly instead of inventing a reading
    assert any(metric.label == "Agreement" and metric.value == "Insufficient evidence" for metric in at.metric)
    assert any("never a score" in caption.value for caption in at.caption)
