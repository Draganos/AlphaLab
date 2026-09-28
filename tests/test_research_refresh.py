"""Deterministic tests for alpha_lab.research_refresh -- the Research
Refresh Orchestrator (roadmap Phase 3, the successor to #52's universe-
tracking model). See that module's own docstring for the full design:
core (price/fundamentals) refresh-if-stale is completely unchanged
(`run_core_refresh`, same function, same tests); everything else is a
read-only, versioned cross-domain evidence status stamp reusing
`alpha_lab.evidence_coverage` (PR #28), never a second coverage engine.
"""
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import (
    CurrentAnalystConsensus,
    CurrentExternalCalibration,
    NewsArticleRecord,
    Price,
    ResearchRefreshStatusSnapshot,
    Security,
)
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.research_refresh import (
    ResearchRefreshOrchestrator,
    get_current_research_refresh_status,
    run_research_refresh_guarded,
)
from alpha_lab.screener import MarketScreenerService


class _FakeProvider(MarketDataProvider):
    """Succeeds for every ticker; tracks every ticker get_price_history
    was actually called for, mirroring tests/test_refresh.py's own fake."""

    provider_name = "FakeTest"

    def __init__(self):
        self.calls: list[str] = []

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        self.calls.append(ticker)
        return pd.DataFrame(
            {"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime([str(date.today())])
        )

    def get_financials(self, ticker):
        return pd.DataFrame()


def _seed_tracked_security(engine, ticker: str, *, price_date: date | None) -> None:
    with Session(engine) as session:
        session.add(Security(ticker=ticker, country="US", currency="USD", is_tracked=True))
        if price_date is not None:
            session.add(Price(
                ticker=ticker, date=price_date, close=10.0, high=11.0, low=9.0,
                provider="fixture", currency="USD", source="test",
            ))
        session.commit()


def _make(monkeypatch, *, tracked: list[tuple[str, date | None]] | None = None):
    """Fresh in-memory DB + settings + fake provider, tracked tickers
    already seeded with fresh prices unless a specific date is given."""
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()
    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    for ticker, price_date in tracked or []:
        _seed_tracked_security(engine, ticker, price_date=price_date)
    return engine, settings, fake


# --- core: unchanged refresh-if-stale semantics ----------------------------


def test_core_is_skipped_when_nothing_is_stale(monkeypatch):
    engine, settings, fake = _make(monkeypatch, tracked=[("NVDA", date.today())])
    status = ResearchRefreshOrchestrator(engine, settings).run()
    assert status.core.skipped is True
    assert fake.calls == []


def test_core_refreshes_when_stale(monkeypatch):
    engine, settings, fake = _make(monkeypatch, tracked=[("NVDA", date.today() - timedelta(days=30))])
    status = ResearchRefreshOrchestrator(engine, settings).run()
    assert status.core.skipped is False
    assert fake.calls == ["NVDA"]
    assert status.core.tickers_succeeded == 1


def test_force_core_refreshes_even_when_fresh(monkeypatch):
    engine, settings, fake = _make(monkeypatch, tracked=[("NVDA", date.today())])
    status = ResearchRefreshOrchestrator(engine, settings).run(force_core=True)
    assert status.core.skipped is False
    assert fake.calls == ["NVDA"]


def test_explicit_tickers_always_attempted_even_when_fresh(monkeypatch):
    """Reproduces the dashboard's own auto-trigger shape: it already
    computed its own stale subset, so this must always attempt it."""
    engine, settings, fake = _make(monkeypatch, tracked=[("NVDA", date.today())])
    status = ResearchRefreshOrchestrator(engine, settings).run(tickers=["NVDA"])
    assert status.core.skipped is False
    assert fake.calls == ["NVDA"]


def test_monkeypatching_run_core_refresh_by_module_path_is_honored(monkeypatch):
    """Regression test for a real bug found during review: an earlier
    version imported `run_core_refresh` via `from alpha_lab.refresh import
    run_core_refresh`, a name captured once at this module's first import.
    Existing tests monkeypatch `alpha_lab.refresh.run_core_refresh` itself
    by string path (see tests/test_refresh.py); a statically-captured name
    elsewhere never sees that patch -- worse, if this module's first-ever
    import happened to occur while such a patch was active, it would
    permanently capture the patched stub for the rest of the test
    process, silently breaking every later test that exercises this
    orchestrator. `_maybe_refresh_core` now looks up `alpha_lab.refresh.
    run_core_refresh` fresh on every call instead."""
    engine, settings, _fake = _make(monkeypatch, tracked=[("NVDA", date.today() - timedelta(days=30))])

    calls = []

    def _stub(engine, settings, tickers=None):
        calls.append(tickers)
        from alpha_lab.refresh import CoreRefreshResult
        return CoreRefreshResult(tickers_attempted=[], tickers_succeeded=[], research_rebuilt=True, research_record_count=0)

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _stub)
    ResearchRefreshOrchestrator(engine, settings).run()
    assert calls == [None]


# --- cross-domain evidence status: read-only, tracked-universe-only -------


def test_domains_exclude_an_untracked_catalog_row(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("TRACKED", date.today())])
    with Session(engine) as session:
        session.add(Security(ticker="CATALOG", country="US", currency="USD", is_tracked=False))
        session.commit()
    MarketScreenerService(engine, settings).rebuild_current_research()

    status = ResearchRefreshOrchestrator(engine, settings).run()

    assert status.tracked_universe_size == 1
    # Every per-security domain's detail counts exactly 1 security, never
    # 2 -- the untracked catalog row must never leak into the aggregate.
    per_security = [d for d in status.domains if d.scope == "per_security"]
    assert per_security
    for domain in per_security:
        assert domain.detail.split("/", 1)[1].startswith("1 ")


def test_analyst_consensus_domain_reflects_stored_evidence(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    with Session(engine) as session:
        session.add(CurrentAnalystConsensus(ticker="AAA", payload={
            "ticker": "AAA", "as_of": date.today().isoformat(), "source": "fixture",
            "strong_buy": 1, "buy": 1, "hold": 0, "sell": 0, "strong_sell": 0,
            "total_analysts": 2, "coverage": 1.0, "confidence": 1.0,
        }))
        session.commit()
    MarketScreenerService(engine, settings).rebuild_current_research()

    status = ResearchRefreshOrchestrator(engine, settings).run()

    row = next(d for d in status.domains if d.domain == "analyst_consensus")
    assert row.status == "FULL"
    assert row.coverage == 1.0
    assert row.freshness is not None


def test_news_domain_reflects_stored_articles(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    with Session(engine) as session:
        session.add(NewsArticleRecord(
            ticker="AAA", content_hash="h1", title="AAA news", url="http://x",
            provider="fixture", published_at=datetime.now(UTC), retrieved_at=datetime.now(UTC),
            raw_payload={},
        ))
        session.commit()
    MarketScreenerService(engine, settings).rebuild_current_research()

    status = ResearchRefreshOrchestrator(engine, settings).run()

    row = next(d for d in status.domains if d.domain == "news")
    assert row.status == "FULL"
    assert row.evidence_count == 1


def test_donatien_domain_is_not_computed_until_refreshed(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    MarketScreenerService(engine, settings).rebuild_current_research()
    status = ResearchRefreshOrchestrator(engine, settings).run()
    row = next(d for d in status.domains if d.domain == "donatien_calibration")
    assert row.status == "NOT_COMPUTED"
    assert row.scope == "global"


def test_donatien_domain_reflects_a_stored_calibration(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    with Session(engine) as session:
        session.add(CurrentExternalCalibration(
            source="Donatien", content_hash="c1", schema_version="v1", source_url="http://x",
            raw_payload={}, normalized_payload={}, retrieved_at=datetime.now(UTC),
        ))
        session.commit()
    MarketScreenerService(engine, settings).rebuild_current_research()

    status = ResearchRefreshOrchestrator(engine, settings).run()

    row = next(d for d in status.domains if d.domain == "donatien_calibration")
    assert row.status == "FULL"
    assert row.freshness is not None


# --- versioning: idempotent, snapshot-on-change only -----------------------


def test_repeated_run_with_no_changes_is_idempotent(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    MarketScreenerService(engine, settings).rebuild_current_research()
    orchestrator = ResearchRefreshOrchestrator(engine, settings)

    first = orchestrator.run()
    second = orchestrator.run()

    assert first.version_id == second.version_id
    with Session(engine) as session:
        snapshots = list(session.scalars(select(ResearchRefreshStatusSnapshot)))
        assert len(snapshots) == 1
        assert snapshots[0].version_id == first.version_id


def test_content_change_creates_a_new_version_and_snapshot(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    MarketScreenerService(engine, settings).rebuild_current_research()
    orchestrator = ResearchRefreshOrchestrator(engine, settings)
    first = orchestrator.run()

    with Session(engine) as session:
        session.add(NewsArticleRecord(
            ticker="AAA", content_hash="h1", title="AAA news", url="http://x",
            provider="fixture", published_at=datetime.now(UTC), retrieved_at=datetime.now(UTC),
            raw_payload={},
        ))
        session.commit()
    second = orchestrator.run()

    assert first.version_id != second.version_id
    with Session(engine) as session:
        total = len(list(session.scalars(select(ResearchRefreshStatusSnapshot))))
        assert total == 2


def test_run_against_an_empty_tracked_universe_never_crashes(monkeypatch):
    """A fresh database, before any backfill, has zero tracked securities
    -- the orchestrator must still produce a valid status (just the two
    global domains, no per-security rows), never raise."""
    engine, settings, fake = _make(monkeypatch)
    status = ResearchRefreshOrchestrator(engine, settings).run()
    assert status.tracked_universe_size == 0
    assert status.core.skipped is True
    assert fake.calls == []
    assert {d.domain for d in status.domains} == {"donatien_calibration", "alignment"}


def test_get_current_research_refresh_status_returns_none_before_any_run(monkeypatch):
    engine, settings, _fake = _make(monkeypatch)
    assert get_current_research_refresh_status(engine) is None


def test_get_current_research_refresh_status_roundtrips(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])
    MarketScreenerService(engine, settings).rebuild_current_research()
    status = ResearchRefreshOrchestrator(engine, settings).run()

    fetched = get_current_research_refresh_status(engine)

    assert fetched is not None
    assert fetched.version_id == status.version_id
    assert fetched.tracked_universe_size == status.tracked_universe_size


# --- concurrency guard: identical shape to run_core_refresh_guarded -------


def test_a_research_refresh_already_in_progress_is_never_duplicated(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today())])

    def _explode(*args, **kwargs):
        raise AssertionError("must not start a second, overlapping research refresh")

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _explode)

    state = {"research_refresh_in_progress": True}
    result = run_research_refresh_guarded(engine, settings, state)

    assert result is None
    assert state["research_refresh_in_progress"] is True  # left exactly as the caller set it


def test_the_in_progress_flag_is_cleared_even_when_the_refresh_raises(monkeypatch):
    engine, settings, _fake = _make(monkeypatch, tracked=[("AAA", date.today() - timedelta(days=30))])

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _explode)

    state: dict[str, object] = {}
    with pytest.raises(RuntimeError):
        run_research_refresh_guarded(engine, settings, state)
    assert state["research_refresh_in_progress"] is False
