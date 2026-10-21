"""Dated tracked-universe history: the percentile scorer must rank a past date
against the universe of that date, and say so when no record exists."""

import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security, UniverseSnapshot
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.refresh import set_tracked_tickers
from alpha_lab.research_state import get_research_state
from alpha_lab.strategy import HistoricalScoringService
from alpha_lab.universe import (
    BASIS_CURRENT_FALLBACK,
    BASIS_LIVE,
    BASIS_SNAPSHOT,
    record_universe_snapshot,
    tracked_universe_as_of,
)

ROOT = Path(__file__).resolve().parents[1]


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    return engine


def _track(engine, *tickers, tracked=True):
    with Session(engine) as session:
        for ticker in tickers:
            security = session.get(Security, ticker)
            if security is None:
                session.add(Security(ticker=ticker, sector="Test", is_tracked=tracked))
            else:
                security.is_tracked = tracked
        session.commit()


def _snapshots(engine):
    with Session(engine) as session:
        return [(s.tickers, s.reason) for s in session.scalars(select(UniverseSnapshot).order_by(UniverseSnapshot.id))]


def _insert_snapshot(engine, recorded_at, tickers):
    with Session(engine) as session:
        session.add(UniverseSnapshot(recorded_at=recorded_at, tickers=sorted(tickers), size=len(tickers), reason="test"))
        session.commit()


# --- recording ------------------------------------------------------------------

def test_a_snapshot_is_recorded_only_when_the_tracked_set_changes():
    engine = _engine()
    assert record_universe_snapshot(engine, "empty") is False  # nothing tracked, nothing to record
    _track(engine, "A", "B")
    assert record_universe_snapshot(engine, "first") is True
    assert record_universe_snapshot(engine, "again") is False
    _track(engine, "C")
    assert record_universe_snapshot(engine, "grew") is True
    _track(engine, "A", "B", "C", tracked=False)
    assert record_universe_snapshot(engine, "emptied") is True  # an emptied universe is real history
    assert _snapshots(engine) == [(["A", "B"], "first"), (["A", "B", "C"], "grew"), ([], "emptied")]


def test_the_history_is_append_only_and_survives_a_second_create_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'u.db'}"
    engine = make_engine(url)
    create_schema(engine)
    _track(engine, "A")
    record_universe_snapshot(engine, "first")
    create_schema(engine)  # must not weaken the triggers or add a duplicate
    engine.dispose()
    raw = sqlite3.connect(tmp_path / "u.db")
    for statement in ("UPDATE universe_snapshots SET size = 0", "DELETE FROM universe_snapshots"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute(statement)
    assert raw.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0] == 1
    raw.close()


def test_a_database_with_a_tracked_universe_but_no_history_gets_an_initial_snapshot_on_create_schema():
    engine = _engine()
    _track(engine, "A", "B")  # tracked before any history existed (an upgraded database)
    assert _snapshots(engine) == []
    create_schema(engine)
    assert _snapshots(engine) == [(["A", "B"], "initial")]
    create_schema(engine)
    assert len(_snapshots(engine)) == 1


class _Provider(MarketDataProvider):
    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": "Fixture", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))

    def get_financials(self, ticker):
        return pd.DataFrame([])


def test_ingesting_a_new_ticker_records_the_change_and_re_ingesting_does_not():
    engine = _engine()
    service = IngestionService(_Provider(), engine)
    service.ingest("AAA", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("AAA", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("BBB", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("MACRO", date(2024, 1, 1), date(2024, 2, 1), mark_tracked=False)
    assert _snapshots(engine) == [(["AAA"], "ingest"), (["AAA", "BBB"], "ingest")]


def test_set_tracked_records_the_new_universe():
    engine = _engine()
    _track(engine, "A", "B", "C")
    record_universe_snapshot(engine, "first")
    set_tracked_tickers(engine, ["A", "C"])
    assert _snapshots(engine)[-1] == (["A", "C"], "set-tracked")


def test_every_code_path_that_changes_is_tracked_records_a_snapshot():
    """Guard: a new writer of the flag that forgets the snapshot would silently
    leave membership history stale. Writers must be on this list and call
    `record_universe_snapshot`."""
    allowed = {
        "alpha_lab/ingestion/service.py", "alpha_lab/refresh.py", "scripts/manage_universe.py",
        "alpha_lab/database/session.py",
    }
    writer = re.compile(r"\.is_tracked\s*=\s*(True|False)\b|SET\s+is_tracked\s*=", re.IGNORECASE)
    found = set()
    for folder in ("alpha_lab", "scripts", "app"):
        for path in (ROOT / folder).rglob("*.py"):
            if writer.search(path.read_text()):
                found.add(str(path.relative_to(ROOT)))
    assert found == allowed, f"unexpected writers of is_tracked: {sorted(found ^ allowed)}"
    for relative in allowed:
        assert "record_universe_snapshot" in (ROOT / relative).read_text(), relative


# --- lookup ----------------------------------------------------------------------

def test_the_universe_on_a_date_is_the_latest_snapshot_recorded_by_then():
    engine = _engine()
    _track(engine, "A", "B", "C")  # today's flag
    _insert_snapshot(engine, datetime(2024, 6, 1, 8, tzinfo=UTC), {"A", "B"})
    _insert_snapshot(engine, datetime(2024, 9, 1, 8, tzinfo=UTC), {"A", "B", "C"})
    today = date(2026, 1, 1)

    before_any = tracked_universe_as_of(engine, date(2024, 5, 31), today=today)
    assert (before_any.basis, before_any.tickers) == (BASIS_CURRENT_FALLBACK, ("A", "B", "C"))
    assert before_any.point_in_time is False

    first = tracked_universe_as_of(engine, date(2024, 6, 1), today=today)  # same day as the snapshot: included
    assert (first.basis, first.tickers) == (BASIS_SNAPSHOT, ("A", "B")) and first.point_in_time
    assert first.recorded_at == datetime(2024, 6, 1, 8, tzinfo=UTC).replace(tzinfo=None)  # stored without a zone

    middle = tracked_universe_as_of(engine, date(2024, 8, 31), today=today)
    assert middle.tickers == ("A", "B")
    later = tracked_universe_as_of(engine, date(2024, 9, 1), today=today)
    assert later.tickers == ("A", "B", "C")

    live = tracked_universe_as_of(engine, today, today=today)
    assert (live.basis, live.tickers) == (BASIS_LIVE, ("A", "B", "C"))


def test_an_unmigrated_database_without_the_table_reports_a_labelled_fallback():
    engine = _engine()
    _track(engine, "A")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE universe_snapshots"))
    result = tracked_universe_as_of(engine, date(2024, 1, 1), today=date(2026, 1, 1))
    assert (result.basis, result.tickers) == (BASIS_CURRENT_FALLBACK, ("A",))


# --- the scorer and research state use it -------------------------------------------

def _prices(session, ticker, slope, *, end=date(2025, 3, 1), bars=330):
    start = end - timedelta(days=bars - 1)
    for offset in range(bars):
        value = 10 + slope * offset
        session.add(Price(ticker=ticker, date=start + timedelta(days=offset), open=value, close=value,
                          adjusted_close=value, volume=1_000, provider="test",
                          ingested_at=datetime(2025, 3, 1, 20, tzinfo=UTC)))


HISTORY = [(datetime(2024, 12, 1, tzinfo=UTC), {"A", "B"}), (datetime(2025, 2, 1, tzinfo=UTC), {"A", "B", "C"})]


def _scored_universe(history=HISTORY, slopes=(("A", 1.0), ("B", 0.5), ("C", 2.0))):
    """A, B and C are all tracked today; `history` says who was tracked when.
    (Securities added after `create_schema` leave no initial snapshot.)"""
    engine = _engine()
    with Session(engine) as session:
        for ticker, slope in slopes:
            session.add(Security(ticker=ticker, sector="Test", is_tracked=True))
            _prices(session, ticker, slope)
        session.commit()
    for recorded_at, tickers in history:
        _insert_snapshot(engine, recorded_at, tickers)
    return engine


def test_the_default_scored_universe_on_a_past_date_is_the_recorded_one_not_todays_flag():
    engine = _scored_universe()
    scorer = HistoricalScoringService(engine, load_settings())
    early = scorer.score_universe_as_of(date(2024, 12, 31), min_score=0, minimum_coverage=0)
    late = scorer.score_universe_as_of(date(2025, 3, 1), min_score=0, minimum_coverage=0)
    assert [r.ticker for r in early] == ["A", "B"]  # C is tracked today but was not then
    assert [r.ticker for r in late] == ["A", "B", "C"]
    # and equal to scoring the recorded members explicitly: C does not move A's or B's percentiles
    explicit = scorer.score_universe_as_of(date(2024, 12, 31), ["A", "B"], min_score=0, minimum_coverage=0)
    assert [(r.ticker, r.score, r.percentile_factors) for r in early] == [(r.ticker, r.score, r.percentile_factors) for r in explicit]
    assert [r.ticker for r in scorer.score_universe_as_of(date.today(), min_score=0, minimum_coverage=0)] == ["A", "B", "C"]
    engine.dispose()


def test_an_explicit_ticker_list_is_never_replaced_by_the_recorded_universe():
    engine = _scored_universe([(datetime(2024, 12, 1, tzinfo=UTC), {"A"})])
    scored = HistoricalScoringService(engine, load_settings()).score_universe_as_of(
        date(2024, 12, 31), ["B", "C"], min_score=0, minimum_coverage=0)
    assert [r.ticker for r in scored] == ["B", "C"]  # a backtest names its own universe
    engine.dispose()


def test_research_state_labels_the_universe_the_score_was_ranked_against():
    engine = _scored_universe()
    settings = load_settings()
    past = get_research_state(engine, settings, "A", date(2024, 12, 31)).deterministic_score
    assert past.value["universe"] == {"basis": "snapshot", "size": 2,
                                      "snapshot_recorded_at": "2024-12-01T00:00:00"}
    assert "universe: snapshot" in past.detail and "ranked against 2 securities" in past.detail
    live = get_research_state(engine, settings, "A").deterministic_score
    assert live.value["universe"]["basis"] == "live" and live.value["universe"]["size"] == 3
    # the universe is part of the provenance: a different peer set is a different id
    other = get_research_state(engine, settings, "A", date(2025, 3, 1)).deterministic_score
    assert other.value["universe"]["size"] == 3 and other.provenance_id != past.provenance_id
    engine.dispose()
    no_history = _scored_universe([])
    fallback = get_research_state(no_history, settings, "A", date(2024, 12, 31)).deterministic_score
    assert fallback.value["universe"]["basis"] == "current_fallback"
    no_history.dispose()
