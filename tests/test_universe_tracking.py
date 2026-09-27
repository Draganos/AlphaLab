"""Deterministic tests for Security.is_tracked -- the live research-universe
membership concept. See Security's own docstring and scripts/manage_universe.py
for the full design: a Security row means only "AlphaLab knows this ticker
exists" (scripts/load_universe.py's broad catalog can create thousands with
no research relationship to AlphaLab at all); is_tracked=True means "this is
part of the live research universe" that Full Refresh, the screener, ethics
classification, and historical scoring actually operate on.
"""
from datetime import date, timedelta
import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.ethics import EthicalClassificationService, load_ethics_policy
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.refresh import configured_universe_tickers
from alpha_lab.screener import MarketScreenerService
from alpha_lab.strategy import HistoricalScoringService

_MANAGE_UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "manage_universe.py"


def _load_manage_universe_module():
    spec = importlib.util.spec_from_file_location("manage_universe_under_test", _MANAGE_UNIVERSE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def manage_universe():
    return _load_manage_universe_module()


_NO_COVERAGE = ProviderError(ProviderErrorKind.NO_DATA, "Fixture", "no coverage")


class _FakeProvider(MarketDataProvider):
    """Succeeds for every ticker except those named in `failing`; returns
    a fresh (today-dated) bar so ingested tickers are never stale. Every
    supplemental domain (analyst consensus/estimates/rating changes/news)
    reports genuine "no coverage" -- a real, honest outcome this codebase
    already treats as a non-failure for e.g. ETFs -- rather than
    implementing each provider surface fully, since these tests exercise
    scripts/manage_universe.py's own orchestration and failure isolation,
    not any one domain's fetch/parse logic (already covered elsewhere)."""

    def __init__(self, failing: dict[str, ProviderError] | None = None):
        self.failing = failing or {}

    def get_company_info(self, ticker):
        if ticker in self.failing:
            raise self.failing[ticker]
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        if ticker in self.failing:
            raise self.failing[ticker]
        return pd.DataFrame(
            {"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime([date.today()])
        )

    def get_financials(self, ticker):
        return pd.DataFrame()

    def get_analyst_consensus(self, ticker):
        raise _NO_COVERAGE

    def get_estimates(self, ticker, observation_date):
        raise _NO_COVERAGE

    def get_analyst_rating_changes(self, ticker):
        raise _NO_COVERAGE

    def get_news(self, ticker):
        raise _NO_COVERAGE

    def get_fund_data(self, ticker):
        return None


# --- IngestionService.ingest: the deliberate membership action ------------


def test_ingest_marks_a_brand_new_security_tracked():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(_FakeProvider(), engine).ingest("NEW", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.get(Security, "NEW").is_tracked is True


def test_ingest_marks_an_existing_untracked_catalog_row_tracked():
    """The scripts/load_universe.py scenario: a Security row exists (cheap
    catalog metadata) but was never a research candidate. Genuinely
    ingesting it (a deliberate act) must promote it."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="CATALOG", country="US", currency="USD", is_tracked=False))
        session.commit()
    IngestionService(_FakeProvider(), engine).ingest("CATALOG", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.get(Security, "CATALOG").is_tracked is True


def test_ingest_with_mark_tracked_false_never_sets_the_flag_on_a_new_security():
    """The MacroRegimeService scenario: a fixed proxy ticker needs price
    history but must never become a research candidate."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(_FakeProvider(), engine).ingest(
        "^VIX", date(2024, 1, 1), date(2024, 2, 1), mark_tracked=False
    )
    with Session(engine) as session:
        assert session.get(Security, "^VIX").is_tracked is False


def test_ingest_with_mark_tracked_false_never_downgrades_an_already_tracked_security():
    """mark_tracked=False only ever skips SETTING the flag; it must never
    clear an already-True flag back to untracked."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="BRK.B", country="US", currency="USD", is_tracked=True))
        session.commit()
    IngestionService(_FakeProvider(), engine).ingest(
        "BRK.B", date(2024, 1, 1), date(2024, 2, 1), mark_tracked=False
    )
    with Session(engine) as session:
        assert session.get(Security, "BRK.B").is_tracked is True


# --- The live-universe read sites: tracked-only ----------------------------


def _seed(engine, ticker: str, *, is_tracked: bool) -> None:
    with Session(engine) as session:
        session.add(Security(
            ticker=ticker, country="US", currency="USD", sector="Technology",
            business_description="A fixture business", metadata_source="fixture",
            is_tracked=is_tracked,
        ))
        session.add(Price(
            ticker=ticker, date=date.today(), close=10.0, high=11.0, low=9.0,
            provider="fixture", currency="USD", source="test",
        ))
        session.commit()


def test_configured_universe_tickers_excludes_an_untracked_catalog_row():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "TRACKED", is_tracked=True)
    _seed(engine, "CATALOG", is_tracked=False)
    assert configured_universe_tickers(engine) == ["TRACKED"]


def test_build_live_records_excludes_an_untracked_catalog_row(monkeypatch):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "TRACKED", is_tracked=True)
    _seed(engine, "CATALOG", is_tracked=False)
    settings = load_settings()
    records = MarketScreenerService(engine, settings).build_live_records()
    assert [record.ticker for record in records] == ["TRACKED"]


def test_score_universe_as_of_with_no_explicit_tickers_excludes_untracked():
    """The live screener's own call shape (tickers=None)."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "TRACKED", is_tracked=True)
    _seed(engine, "CATALOG", is_tracked=False)
    settings = load_settings()
    scores = HistoricalScoringService(engine, settings).score_universe_as_of(date.today())
    assert [score.ticker for score in scores] == ["TRACKED"]


def test_score_universe_as_of_with_explicit_tickers_ignores_tracking_status():
    """The backtester's own call shape (an explicit, named universe) --
    must score exactly what it's given regardless of is_tracked, since a
    backtest study may legitimately name a ticker AlphaLab never marked as
    a live research candidate."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "TRACKED", is_tracked=True)
    _seed(engine, "CATALOG", is_tracked=False)
    settings = load_settings()
    scores = HistoricalScoringService(engine, settings).score_universe_as_of(
        date.today(), tickers=["CATALOG"]
    )
    assert [score.ticker for score in scores] == ["CATALOG"]


def test_ethical_classification_ensure_all_excludes_an_untracked_catalog_row():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "TRACKED", is_tracked=True)
    _seed(engine, "CATALOG", is_tracked=False)
    results = EthicalClassificationService(engine, load_ethics_policy()).ensure_all()
    assert set(results.keys()) == {"TRACKED"}


# --- scripts/manage_universe.py: the explicit add/remove entrypoint -------


def test_manage_universe_add_marks_tracked_and_rebuilds_research(monkeypatch, manage_universe):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()
    monkeypatch.setattr(manage_universe, "YFinanceProvider", lambda: _FakeProvider())

    ok = manage_universe.add_tickers(engine, settings, ["NEWCO"])

    assert ok is True
    with Session(engine) as session:
        security = session.get(Security, "NEWCO")
        assert security is not None
        assert security.is_tracked is True
    assert configured_universe_tickers(engine) == ["NEWCO"]


class _AnalystConsensusSucceedsProvider(_FakeProvider):
    """Unlike `_FakeProvider`, analyst consensus genuinely succeeds here --
    exercising `SupplementalResearchService.refresh_all`'s AI-Research-
    Rating branch, which only runs when analyst consensus does not fail
    (see that method's own docstring). Regression coverage for a real bug
    found during review: an earlier version of `add_tickers` called
    `ResearchService.get_stock_research` before any research rebuild had
    ever run for a freshly-ingested ticker, so it was always `None` on a
    ticker's first `add` -- silently routing every brand-new ticker into
    the analyst-consensus-only branch and never computing an AI Research
    Rating at all, no matter how the provider behaved."""

    def get_analyst_consensus(self, ticker):
        return {
            "strong_buy": 5, "buy": 3, "hold": 1, "sell": 0, "strong_sell": 0,
            "target_current": 100.0, "target_low": 80.0, "target_mean": 110.0,
            "target_median": 108.0, "target_high": 130.0, "as_of": date.today(),
        }


def test_manage_universe_add_computes_ai_research_rating_on_first_add(monkeypatch, manage_universe):
    """A brand-new ticker's AI Research Rating must be computed within its
    very first `add` call, not silently deferred to a second one."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()
    monkeypatch.setattr(
        manage_universe, "YFinanceProvider", lambda: _AnalystConsensusSucceedsProvider()
    )

    ok = manage_universe.add_tickers(engine, settings, ["NEWCO"])

    assert ok is True
    from alpha_lab.research import ResearchService

    research = ResearchService(engine, settings).get_stock_research("NEWCO")
    assert research is not None
    assert research.ai_research_assessment is not None


def test_manage_universe_add_reports_failure_without_crashing(monkeypatch, manage_universe):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()
    failing = {"BAD": ProviderError(ProviderErrorKind.NO_DATA, "Fixture", "no data")}
    monkeypatch.setattr(manage_universe, "YFinanceProvider", lambda: _FakeProvider(failing))

    ok = manage_universe.add_tickers(engine, settings, ["BAD"])

    assert ok is False
    with Session(engine) as session:
        assert session.get(Security, "BAD") is None


def test_manage_universe_remove_flips_the_flag_without_deleting_history(monkeypatch, manage_universe):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "GONE", is_tracked=True)
    settings = load_settings()

    ok = manage_universe.remove_tickers(engine, settings, ["GONE"])

    assert ok is True
    with Session(engine) as session:
        security = session.get(Security, "GONE")
        assert security is not None
        assert security.is_tracked is False
        # History is never touched by removal.
        assert session.scalar(select(Price).where(Price.ticker == "GONE")) is not None
    assert configured_universe_tickers(engine) == []


def test_manage_universe_remove_reports_an_unknown_ticker(monkeypatch, manage_universe):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()

    ok = manage_universe.remove_tickers(engine, settings, ["NOPE"])

    assert ok is False


def test_manage_universe_remove_is_idempotent(monkeypatch, manage_universe):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed(engine, "GONE", is_tracked=True)
    settings = load_settings()

    first = manage_universe.remove_tickers(engine, settings, ["GONE"])
    second = manage_universe.remove_tickers(engine, settings, ["GONE"])

    assert first is True
    assert second is True  # already untracked is a no-op, not a failure
    with Session(engine) as session:
        assert session.get(Security, "GONE").is_tracked is False


# --- Migration: additive, idempotent, never destroys existing data --------


def test_is_tracked_migration_is_additive_and_defaults_existing_rows_to_untracked():
    """A database created before this column existed must upgrade in
    place -- never require deleting/recreating the database -- with every
    pre-existing row correctly defaulting to untracked (never silently
    promoted into the live universe by the migration itself)."""
    from sqlalchemy import text

    engine = make_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE securities (
                ticker VARCHAR(32) PRIMARY KEY, company_name VARCHAR(255),
                exchange VARCHAR(64), country VARCHAR(64), sector VARCHAR(128),
                currency VARCHAR(8), asset_type VARCHAR(64)
            )
        """))
        connection.execute(text(
            "INSERT INTO securities (ticker, country, currency) VALUES ('LEGACY', 'US', 'USD')"
        ))
    create_schema(engine)
    create_schema(engine)  # idempotent: safe to run more than once
    with Session(engine) as session:
        security = session.get(Security, "LEGACY")
        assert security is not None
        assert security.is_tracked is False
