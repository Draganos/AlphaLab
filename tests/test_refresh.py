"""Deterministic tests for alpha_lab.refresh: the canonical core-refresh
(price/fundamental ingestion + current-research rebuild) operation shared
by scripts/launch.py's auto-refresh-on-launch check and the main
dashboard's Full Refresh button.

No network -- every provider call goes through a fake MarketDataProvider,
mirroring tests/test_load_us_data.py's established pattern.
"""

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import select

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.refresh import (
    CoreRefreshResult,
    configured_universe_tickers,
    is_universe_price_stale,
    run_core_refresh,
    run_core_refresh_guarded,
)
from alpha_lab.screener import LiveResearchRecord


class _FakeProvider(MarketDataProvider):
    """Succeeds for every ticker except those named in `failing`; tracks
    every ticker `get_price_history` was actually called for."""

    def __init__(self, failing: dict[str, ProviderError] | None = None):
        self.failing = failing or {}
        self.calls: list[str] = []

    def get_company_info(self, ticker):
        if ticker in self.failing:
            raise self.failing[ticker]
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        self.calls.append(ticker)
        if ticker in self.failing:
            raise self.failing[ticker]
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))

    def get_financials(self, ticker):
        return pd.DataFrame()


def _seed_security_with_price(engine, ticker: str, price_date: date) -> None:
    with Session(engine) as session:
        session.add(Security(ticker=ticker, country="US", currency="USD", is_tracked=True))
        session.add(Price(
            ticker=ticker, date=price_date, close=100.0, high=101.0, low=99.0,
            provider="fixture", currency="USD", source="test",
        ))
        session.commit()


def _seed_current_research(engine, *, ticker: str = "NVDA") -> None:
    record = LiveResearchRecord(
        ticker=ticker, company="Fixture Inc.", price=100.0, market_cap=1_000_000_000,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="EQUITY", ethical_status="PASS", data_quality_status="valid",
        overall_score=55.0, category_scores={}, category_coverage={},
        raw_metrics={}, percentile_metrics={}, overall_live_coverage=0.5,
        quantitative_coverage=0.5, ai_coverage=0.5, historical_coverage=0.5,
        confidence="Moderate", provenance={}, last_refreshed=None,
        configuration_hash="fixture-refresh-test", evaluation_date=date(2026, 1, 1),
    )
    Phase3Repository(engine).save_current_research([record])


# --- is_universe_price_stale -------------------------------------------------

def test_fresh_price_data_is_not_stale():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today())
    assert is_universe_price_stale(engine, stale_after_days=7) is False


def test_price_older_than_the_observation_limit_is_stale():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today() - timedelta(days=30))
    assert is_universe_price_stale(engine, stale_after_days=7) is True


def test_a_tracked_security_with_no_price_at_all_is_stale():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="NVDA", country="US", currency="USD", is_tracked=True))
        session.commit()
    assert is_universe_price_stale(engine, stale_after_days=7) is True


def test_an_empty_universe_is_never_stale():
    """Nothing tracked yet is a bootstrapping concern, not a staleness one
    -- see is_universe_price_stale's docstring."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    assert is_universe_price_stale(engine, stale_after_days=7) is False


# --- run_core_refresh: stale -> exactly one refresh --------------------------

def test_stale_data_triggers_exactly_one_ingestion_call_per_ticker(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today() - timedelta(days=30))
    _seed_security_with_price(engine, "AAPL", date.today() - timedelta(days=30))
    settings = load_settings()

    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    monkeypatch.setattr(
        "alpha_lab.refresh.MarketScreenerService.rebuild_current_research",
        lambda self: [],
    )

    assert is_universe_price_stale(engine, stale_after_days=7) is True
    result = run_core_refresh(engine, settings)

    assert sorted(fake.calls) == ["AAPL", "NVDA"]
    assert sorted(result.tickers_succeeded) == ["AAPL", "NVDA"]
    assert result.tickers_failed == {}
    assert result.research_rebuilt is True


def test_run_core_refresh_batches_a_universe_larger_than_the_cap(monkeypatch):
    """Real incident this guards against: scripts/load_universe.py can load
    thousands of Security rows with zero price data (all instantly stale),
    and run_core_refresh(tickers=None) used to attempt every single one in
    one blocking call. Above MAX_FULL_UNIVERSE_REFRESH_BATCH, it must
    instead ingest only that many of the stalest tickers -- alphabetical
    order, matching stale_universe_tickers' own ordering -- leaving the
    rest for a later call rather than turning one click into a many-hour
    operation."""
    import alpha_lab.refresh as refresh_module

    monkeypatch.setattr(refresh_module, "MAX_FULL_UNIVERSE_REFRESH_BATCH", 3)
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
            session.add(Security(ticker=ticker, country="US", currency="USD", is_tracked=True))
        session.commit()
    settings = load_settings()

    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    monkeypatch.setattr(
        "alpha_lab.refresh.MarketScreenerService.rebuild_current_research",
        lambda self: [],
    )

    result = run_core_refresh(engine, settings, tickers=None)

    assert result.tickers_attempted == ["AAA", "BBB", "CCC"]
    assert fake.calls == ["AAA", "BBB", "CCC"]
    assert result.research_rebuilt is True


def test_repeated_calls_work_through_a_large_stale_backlog_without_double_processing(monkeypatch):
    """Self-correcting batching, no persisted cursor: once a batch's tickers
    are ingested they are no longer stale, so the next call's stale set
    naturally advances to the next alphabetical group. Uses a fresh-dated
    fake (unlike the shared _FakeProvider's fixed 2024-01-01 bar) because
    that self-correction depends on the newly-ingested price actually being
    recent enough to clear the staleness check -- exactly what the real
    YFinanceProvider returns (data through `end=date.today()`)."""
    import alpha_lab.refresh as refresh_module

    class _FreshProvider(MarketDataProvider):
        def __init__(self):
            self.calls: list[str] = []

        def get_company_info(self, ticker):
            return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

        def get_price_history(self, ticker, start, end):
            self.calls.append(ticker)
            return pd.DataFrame(
                {"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime([date.today()])
            )

        def get_financials(self, ticker):
            return pd.DataFrame()

    monkeypatch.setattr(refresh_module, "MAX_FULL_UNIVERSE_REFRESH_BATCH", 3)
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
            session.add(Security(ticker=ticker, country="US", currency="USD", is_tracked=True))
        session.commit()
    settings = load_settings()

    fake = _FreshProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    monkeypatch.setattr(
        "alpha_lab.refresh.MarketScreenerService.rebuild_current_research",
        lambda self: [],
    )

    first = run_core_refresh(engine, settings, tickers=None)
    second = run_core_refresh(engine, settings, tickers=None)

    assert first.tickers_attempted == ["AAA", "BBB", "CCC"]
    assert second.tickers_attempted == ["DDD", "EEE"]
    assert fake.calls == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_configured_universe_tickers_matches_what_rebuild_current_research_reads():
    """run_core_refresh must ingest exactly the tickers
    MarketScreenerService.build_live_records itself reads from Security --
    never a rediscovered/expanded universe."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today())
    _seed_security_with_price(engine, "AAPL", date.today())
    assert sorted(configured_universe_tickers(engine)) == ["AAPL", "NVDA"]


# --- refresh failure preserves existing valid state --------------------------

def test_a_failed_research_rebuild_never_touches_previously_persisted_research(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today() - timedelta(days=30))
    _seed_current_research(engine, ticker="NVDA")
    settings = load_settings()

    before_build, before_payloads = Phase3Repository(engine).latest_current_payloads()

    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)

    def _raise(self):
        raise RuntimeError("simulated rebuild failure")

    monkeypatch.setattr("alpha_lab.refresh.MarketScreenerService.rebuild_current_research", _raise)

    result = run_core_refresh(engine, settings)  # must not raise

    assert result.research_rebuilt is False
    assert result.research_error == "simulated rebuild failure"

    after_build, after_payloads = Phase3Repository(engine).latest_current_payloads()
    assert after_build.id == before_build.id
    assert after_payloads == before_payloads


def test_a_ticker_ingestion_failure_never_aborts_the_rest_or_the_rebuild(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today() - timedelta(days=30))
    _seed_security_with_price(engine, "BAD", date.today() - timedelta(days=30))
    settings = load_settings()

    failing = {"BAD": ProviderError(ProviderErrorKind.RATE_LIMITED, "Fixture", "rate limited")}
    fake = _FakeProvider(failing)
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    monkeypatch.setattr(
        "alpha_lab.refresh.MarketScreenerService.rebuild_current_research",
        lambda self: [],
    )

    result = run_core_refresh(engine, settings)

    assert result.tickers_succeeded == ["NVDA"]
    assert "BAD" in result.tickers_failed
    assert result.research_rebuilt is True  # the rebuild step still runs


# --- normal Streamlit reruns never trigger a provider -------------------------

def test_importing_the_dashboard_normally_never_calls_core_refresh(tmp_path, monkeypatch):
    """A plain module import/exec of app/dashboard/main.py (what a normal
    Streamlit rerun does) must never invoke run_core_refresh -- only an
    actual button click (which importlib-based import can't simulate,
    since st.button always returns False outside a live ScriptRunContext)
    may. Monkeypatching run_core_refresh to raise proves it every rerun
    Streamlit does on its own stays a pure read."""
    import importlib.util
    from pathlib import Path

    db_path = tmp_path / "dashboard.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    engine.dispose()

    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")

    def _explode(*args, **kwargs):
        raise AssertionError("run_core_refresh must never be called on a normal rerun")

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _explode)
    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh_guarded", _explode)

    main_path = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"
    spec = importlib.util.spec_from_file_location("dashboard_main_refresh_test", main_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not raise
    module.engine.dispose()


# --- Full Refresh button and the guard against duplicates --------------------

def test_full_refresh_invokes_the_same_core_refresh_path(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today())
    settings = load_settings()

    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)
    monkeypatch.setattr(
        "alpha_lab.refresh.MarketScreenerService.rebuild_current_research",
        lambda self: [],
    )

    state: dict[str, object] = {}
    result = run_core_refresh_guarded(engine, settings, state)

    assert isinstance(result, CoreRefreshResult)
    assert fake.calls == ["NVDA"]  # the same provider path run_core_refresh itself takes
    assert state["core_refresh_in_progress"] is False  # cleared again after completion


def test_a_refresh_already_in_progress_is_never_duplicated(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security_with_price(engine, "NVDA", date.today())
    settings = load_settings()

    def _explode(*args, **kwargs):
        raise AssertionError("must not start a second, overlapping core refresh")

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _explode)

    state = {"core_refresh_in_progress": True}
    result = run_core_refresh_guarded(engine, settings, state)

    assert result is None
    assert state["core_refresh_in_progress"] is True  # left exactly as the caller set it


def test_the_in_progress_flag_is_cleared_even_when_the_refresh_raises(monkeypatch):
    """A crashed refresh must never permanently lock out future attempts."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("alpha_lab.refresh.run_core_refresh", _explode)

    state: dict[str, object] = {}
    with pytest.raises(RuntimeError):
        run_core_refresh_guarded(engine, settings, state)
    assert state["core_refresh_in_progress"] is False


def test_filing_eligible_tickers_are_tracked_non_etfs_only():
    from alpha_lab.refresh import filing_eligible_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add_all([
            Security(ticker="MSFT", asset_type="EQUITY", is_tracked=True),
            Security(ticker="GDX", asset_type="ETF", is_tracked=True),
            Security(ticker="OLD", asset_type="EQUITY", is_tracked=False),
            Security(ticker="ODD", asset_type=None, is_tracked=True),
        ])
        session.commit()
    assert filing_eligible_tickers(engine) == ["MSFT", "ODD"]


def test_refresh_scripts_default_to_the_tracked_universe_not_the_config_list():
    """Regression: refresh_supplemental_research/estimates/estimate_revisions/
    analyst_events defaulted to `settings.universe["us"]` (config/default.yaml),
    a second live-universe mechanism that drifts from `Security.is_tracked`
    -- a ticker added via manage_universe.py was never refreshed by them."""
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in (
        "refresh_supplemental_research", "refresh_estimates",
        "refresh_estimate_revisions", "refresh_analyst_events",
    ):
        source = (scripts / f"{name}.py").read_text()
        assert "settings.universe" not in source, name
        assert "configured_universe_tickers(engine)" in source, name
