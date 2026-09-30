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
from alpha_lab.phase3 import Phase3Repository
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

    def get_estimate_revision_trend(self, ticker, observation_date):
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


# --- coverage-gap fixes: `add` must not silently skip revision trend / filings ---


class _RevisionTrendProvider(_FakeProvider):
    provider_name = "FakeRevisionProvider"

    def get_estimate_revision_trend(self, ticker, observation_date):
        return [
            {
                "fiscal_period": date(2027, 1, 25),
                "eps_trend_current": 9.30456, "eps_trend_7d_ago": 9.30741,
                "eps_trend_30d_ago": 8.96264, "eps_trend_60d_ago": 8.9416,
                "eps_trend_90d_ago": 8.92355,
                "revisions_up_last_7d": 2, "revisions_up_last_30d": 39,
                "revisions_down_last_7d": 0, "revisions_down_last_30d": 1,
                "currency": "USD",
            }
        ]


def test_manage_universe_add_fetches_the_estimate_revision_trend(monkeypatch, manage_universe):
    """Regression: `add` fetched estimates and rating changes but never the
    EPS revision trend, so every ticker added this way (MSFT, AAPL, ...)
    had zero EstimateRevisionTrend rows despite the provider returning
    them instantly."""
    from alpha_lab.database.models import EstimateRevisionTrend

    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    monkeypatch.delenv("ALPHALAB_SEC_USER_AGENT", raising=False)
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    monkeypatch.setattr(manage_universe, "YFinanceProvider", lambda: _RevisionTrendProvider())

    assert manage_universe.add_tickers(engine, load_settings(), ["NEWCO"]) is True

    with Session(engine) as session:
        rows = session.scalars(select(EstimateRevisionTrend).where(EstimateRevisionTrend.ticker == "NEWCO")).all()
    assert len(rows) >= 1


def test_manage_universe_add_reports_a_revision_trend_failure_without_aborting(monkeypatch, manage_universe, capsys):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    monkeypatch.delenv("ALPHALAB_SEC_USER_AGENT", raising=False)
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    monkeypatch.setattr(manage_universe, "YFinanceProvider", lambda: _FakeProvider())

    assert manage_universe.add_tickers(engine, load_settings(), ["NEWCO"]) is True
    assert "estimate revision trend: FAILED" in capsys.readouterr().out


def _seed_security(engine, ticker: str, asset_type: str) -> None:
    with Session(engine) as session:
        session.add(Security(ticker=ticker, asset_type=asset_type, is_tracked=True))
        session.commit()


def test_ingest_filings_says_so_when_the_sec_user_agent_is_missing(monkeypatch, manage_universe, capsys):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security(engine, "MSFT", "EQUITY")
    monkeypatch.delenv("ALPHALAB_SEC_USER_AGENT", raising=False)

    manage_universe._ingest_filings(engine, "MSFT")

    out = capsys.readouterr().out
    assert "SKIPPED" in out and "ALPHALAB_SEC_USER_AGENT" in out


def test_ingest_filings_skips_an_etf_without_contacting_the_sec(monkeypatch, manage_universe, capsys):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security(engine, "GDX", "ETF")
    monkeypatch.setenv("ALPHALAB_SEC_USER_AGENT", "Test test@example.com")

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("an ETF must never trigger an SEC request")

    monkeypatch.setattr(manage_universe, "ingest_company_documents", _must_not_be_called)
    manage_universe._ingest_filings(engine, "GDX")
    assert "not applicable (ETF" in capsys.readouterr().out


def test_ingest_filings_ingests_an_equity_and_isolates_a_sec_failure(monkeypatch, manage_universe, capsys):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_security(engine, "MSFT", "EQUITY")
    monkeypatch.setenv("ALPHALAB_SEC_USER_AGENT", "Test test@example.com")
    monkeypatch.setattr(manage_universe, "SECClient", lambda ua: object())
    monkeypatch.setattr(manage_universe, "SECFilingDocumentProvider", lambda client: object())
    monkeypatch.setattr(manage_universe, "ingest_company_documents", lambda engine, provider, ticker: 3)
    manage_universe._ingest_filings(engine, "MSFT")
    assert "stored 3 new filing document(s)" in capsys.readouterr().out

    def _boom(engine, provider, ticker):
        raise RuntimeError("SEC down")

    monkeypatch.setattr(manage_universe, "ingest_company_documents", _boom)
    manage_universe._ingest_filings(engine, "MSFT")  # must not raise
    assert "FAILED (SEC down); prior data preserved" in capsys.readouterr().out


# --- empty tracked universe after the is_tracked migration ---------------


def _legacy_database_without_is_tracked(path, tickers, *, built_tickers):
    """A database as it existed before `securities.is_tracked`: the column is
    absent, and the latest current research build contains `built_tickers`."""
    import sqlite3

    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    engine.dispose()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE securities DROP COLUMN is_tracked")
    for ticker in tickers:
        con.execute("INSERT INTO securities (ticker) VALUES (?)", (ticker,))
    con.execute(
        "INSERT INTO current_research_builds (id, evaluation_date, built_at, score_version, config_hash, security_count) "
        "VALUES (1, '2026-09-01', '2026-09-01 00:00:00', 'v', 'h', ?)", (len(built_tickers),),
    )
    for ticker in built_tickers:
        con.execute(
            "INSERT INTO current_research_snapshots (build_id, ticker, payload) VALUES (1, ?, '{}')", (ticker,)
        )
    con.commit()
    con.close()


def test_migration_keeps_the_latest_builds_securities_tracked(tmp_path):
    """Regression: the column was added with DEFAULT 0 and no backfill, so
    every pre-existing security silently left the live universe -- Full
    Refresh then found 0 tickers ("Ingested 0/0 ... rebuilt for 0")."""
    path = tmp_path / "legacy.db"
    _legacy_database_without_is_tracked(path, ["MSFT", "GDX", "CATALOG1", "CATALOG2"], built_tickers=["MSFT", "GDX"])

    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    assert configured_universe_tickers(engine) == ["GDX", "MSFT"]
    with Session(engine) as session:
        assert session.get(Security, "CATALOG1").is_tracked is False


def test_migration_backfill_runs_only_when_the_column_is_first_added(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_database_without_is_tracked(path, ["MSFT", "GDX"], built_tickers=["MSFT", "GDX"])
    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    with Session(engine) as session:
        session.get(Security, "GDX").is_tracked = False  # a deliberate `remove`
        session.commit()
    create_schema(engine)  # e.g. the next script start
    assert configured_universe_tickers(engine) == ["MSFT"]


def test_adopt_current_research_tickers_only_adds_and_is_idempotent(tmp_path):
    from alpha_lab.refresh import adopt_current_research_tickers

    path = tmp_path / "migrated.db"
    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    with Session(engine) as session:
        for ticker in ("MSFT", "GDX", "OTHER"):
            session.add(Security(ticker=ticker, is_tracked=False))
        session.commit()
    Phase3Repository(engine).save_current_research([_adopt_record("MSFT"), _adopt_record("GDX")])

    assert adopt_current_research_tickers(engine) == ["GDX", "MSFT"]
    assert configured_universe_tickers(engine) == ["GDX", "MSFT"]
    assert adopt_current_research_tickers(engine) == []  # nothing left to adopt
    with Session(engine) as session:
        assert session.get(Security, "OTHER").is_tracked is False


def test_adopt_current_research_tickers_with_no_build_does_nothing():
    from alpha_lab.refresh import adopt_current_research_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="MSFT", is_tracked=False))
        session.commit()
    assert adopt_current_research_tickers(engine) == []


def _adopt_record(ticker):
    from alpha_lab.research import CATEGORY_ORDER
    from alpha_lab.screener import LiveResearchRecord

    return LiveResearchRecord(
        ticker=ticker, company=f"{ticker} Inc", price=100.0, market_cap=1_000.0,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="equity", themes=[], ethical_status="PASS", data_quality_status="valid",
        overall_score=70.0, overall_rank=1,
        category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={}, percentile_metrics={}, overall_live_coverage=0.5,
        quantitative_coverage=0.5, ai_coverage=0.0, historical_coverage=0.0,
        confidence="Moderate", provenance={}, last_refreshed=None,
        rating_version="test-v1", configuration_hash="test-config", evaluation_date=date.today(),
    )


def test_manage_universe_adopt_current_command(monkeypatch, manage_universe, capsys):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="MSFT", is_tracked=False))
        session.commit()
    Phase3Repository(engine).save_current_research([_adopt_record("MSFT")])

    assert manage_universe.adopt_current(engine) is True
    assert "Re-tracked 1" in capsys.readouterr().out
    assert configured_universe_tickers(engine) == ["MSFT"]
    assert manage_universe.adopt_current(engine) is False  # idempotent


# --- adopting an oversized build; set-tracked --------------------------------


def test_adopt_refuses_a_build_larger_than_a_curated_universe():
    """Regression: on a real database the latest build held 5,149 securities
    (the pre-tracking 'every security' universe) and adopting it recreated
    exactly the bloat `is_tracked` exists to end."""
    from alpha_lab.refresh import UniverseTooLargeToAdopt, adopt_current_research_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    tickers = ["AAA", "BBB", "CCC"]
    with Session(engine) as session:
        for ticker in tickers:
            session.add(Security(ticker=ticker, is_tracked=False))
        session.commit()
    Phase3Repository(engine).save_current_research([_adopt_record(t) for t in tickers])

    with pytest.raises(UniverseTooLargeToAdopt):
        adopt_current_research_tickers(engine, max_tickers=2)
    assert configured_universe_tickers(engine) == []  # nothing changed
    assert adopt_current_research_tickers(engine, max_tickers=3) == tickers


def test_migration_backfill_is_skipped_for_an_oversized_build(tmp_path):
    from alpha_lab.database.session import _MAX_BACKFILL_TRACKED

    path = tmp_path / "legacy_big.db"
    tickers = [f"T{i:04d}" for i in range(_MAX_BACKFILL_TRACKED + 1)]
    _legacy_database_without_is_tracked(path, tickers, built_tickers=tickers)
    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    assert configured_universe_tickers(engine) == []


def test_backfill_cap_matches_the_full_refresh_cap():
    from alpha_lab.database.session import _MAX_BACKFILL_TRACKED
    from alpha_lab.refresh import MAX_FULL_UNIVERSE_REFRESH_BATCH

    assert _MAX_BACKFILL_TRACKED == MAX_FULL_UNIVERSE_REFRESH_BATCH


def test_set_tracked_makes_the_universe_exactly_the_named_tickers():
    from alpha_lab.refresh import set_tracked_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker in ("MSFT", "GDX", "JUNK1", "JUNK2"):
            session.add(Security(ticker=ticker, is_tracked=ticker.startswith("JUNK")))
        session.commit()

    result = set_tracked_tickers(engine, ["msft", "GDX", "SPY", "MSFT"])

    assert configured_universe_tickers(engine) == ["GDX", "MSFT"]
    assert result.newly_tracked == ["GDX", "MSFT"]
    assert result.untracked_count == 2
    assert result.missing == ["SPY"]
    with Session(engine) as session:
        assert session.get(Security, "JUNK1") is not None  # flag flipped, row kept
        assert session.get(Security, "JUNK1").is_tracked is False
    assert set_tracked_tickers(engine, ["MSFT", "GDX"]).untracked_count == 0  # idempotent


def test_set_tracked_never_leaves_the_universe_empty():
    from alpha_lab.refresh import set_tracked_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="MSFT", is_tracked=True))
        session.commit()
    with pytest.raises(ValueError):
        set_tracked_tickers(engine, ["TYPO"])
    assert configured_universe_tickers(engine) == ["MSFT"]


def test_manage_universe_set_tracked_command(monkeypatch, manage_universe, capsys):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="MSFT", is_tracked=False))
        session.add(Security(ticker="JUNK", is_tracked=True))
        session.commit()

    ok = manage_universe.set_tracked(engine, load_settings(), ["MSFT", "SPY"])

    out = capsys.readouterr().out
    assert ok is False  # SPY is unknown: reported, and the exit status says so
    assert "NOT FOUND" in out and "add SPY" in out
    assert configured_universe_tickers(engine) == ["MSFT"]


def test_manage_universe_adopt_current_reports_an_oversized_build(monkeypatch, manage_universe, capsys):
    from alpha_lab.refresh import UniverseTooLargeToAdopt

    def _too_big(engine):
        raise UniverseTooLargeToAdopt(5149, 200)

    monkeypatch.setattr(manage_universe, "adopt_current_research_tickers", _too_big)
    assert manage_universe.adopt_current(make_engine("sqlite:///:memory:")) is False
    assert "set-tracked" in capsys.readouterr().out


def test_adopt_cap_bounds_the_whole_build_not_just_the_untracked_part():
    """Regression (review finding on #60): with 2 already-tracked + 2
    untracked securities in the build and a cap of 3, bounding only the
    untracked candidates let adoption through and grew the universe to 4."""
    from alpha_lab.refresh import UniverseTooLargeToAdopt, adopt_current_research_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    with Session(engine) as session:
        for ticker in tickers:
            session.add(Security(ticker=ticker, is_tracked=ticker in ("AAA", "BBB")))
        session.commit()
    Phase3Repository(engine).save_current_research([_adopt_record(t) for t in tickers])

    with pytest.raises(UniverseTooLargeToAdopt):
        adopt_current_research_tickers(engine, max_tickers=3)
    assert configured_universe_tickers(engine) == ["AAA", "BBB"]  # unchanged


def test_adopt_cap_bounds_the_resulting_tracked_universe_including_tickers_outside_the_build():
    from alpha_lab.refresh import UniverseTooLargeToAdopt, adopt_current_research_tickers

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker in ("OUT1", "OUT2"):  # tracked, but not in the latest build
            session.add(Security(ticker=ticker, is_tracked=True))
        for ticker in ("AAA", "BBB"):
            session.add(Security(ticker=ticker, is_tracked=False))
        session.commit()
    Phase3Repository(engine).save_current_research([_adopt_record("AAA"), _adopt_record("BBB")])

    with pytest.raises(UniverseTooLargeToAdopt):
        adopt_current_research_tickers(engine, max_tickers=3)  # 2 tracked + 2 adopted = 4
    assert adopt_current_research_tickers(engine, max_tickers=4) == ["AAA", "BBB"]
