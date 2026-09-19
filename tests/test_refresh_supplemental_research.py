"""Regression test for scripts/refresh_supplemental_research.py's
automatic historical snapshotting (roadmap: historical research
reconstruction, see ARCHITECTURE.md).

Self-review finding: an earlier version of this feature wired automatic
snapshot-on-refresh into the Company Research page's "Refresh for this
ticker" button only, silently leaving this script's own routine/batch use
-- plausibly the primary way supplemental research is refreshed for a
whole universe in practice -- out of history entirely. Both now call the
same ResearchService.snapshot_current_research."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.research import ResearchService, CATEGORY_ORDER
from alpha_lab.screener import LiveResearchRecord

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_supplemental_research.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("refresh_supplemental_research_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProvider(MarketDataProvider):
    provider_name = "FakeTest"

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        return pd.DataFrame(
            {"close": [100.0 + i for i in range(60)]},
            index=pd.date_range("2026-01-01", periods=60),
        )

    def get_financials(self, ticker):
        return pd.DataFrame()

    def get_analyst_consensus(self, ticker):
        return {
            "strong_buy": 5, "buy": 3, "hold": 1, "sell": 0, "strong_sell": 0,
            "target_current": 100.0, "target_low": 80.0, "target_mean": 120.0,
            "target_median": 118.0, "target_high": 150.0, "as_of": date.today(), "source": "FakeTest",
        }

    def get_fund_data(self, ticker):
        return None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "refresh_supplemental.db"
    engine = make_engine(f"sqlite:///{path}")
    create_schema(engine)
    with_security = Security(ticker="NVDA", country="US", currency="USD")
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        session.add(with_security)
        session.commit()
    record = LiveResearchRecord(
        ticker="NVDA", company="Nvidia Corp", price=100.0, market_cap=1_000_000_000,
        country="US", exchange="NASDAQ", sector="Technology", industry="Semiconductors",
        asset_type="EQUITY", ethical_status="PASS", data_quality_status="valid",
        overall_score=55.0, category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={}, percentile_metrics={}, overall_live_coverage=0.5,
        quantitative_coverage=0.5, ai_coverage=0.5, historical_coverage=0.5,
        confidence="Moderate", provenance={}, last_refreshed=None,
        configuration_hash="fixture-test", evaluation_date=date.today(),
    )
    Phase3Repository(engine).save_current_research([record])
    engine.dispose()
    return path


def test_main_persists_an_automatic_snapshot_for_a_refreshed_ticker(db_path, monkeypatch):
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    module = _load_script_module()
    monkeypatch.setattr(module, "YFinanceProvider", _FakeProvider)
    monkeypatch.setattr(sys, "argv", ["refresh_supplemental_research.py", "NVDA"])

    settings = load_settings()
    engine = make_engine(f"sqlite:///{db_path}")
    service = ResearchService(engine, settings)
    assert service.get_research_history("NVDA") == []

    exit_code = module.main()

    assert exit_code == 0
    history = service.get_research_history("NVDA")
    assert len(history) == 1
    snapshot = service.get_research_snapshot(history[0].snapshot_id)
    assert snapshot.analyst_consensus is not None
    assert snapshot.technical_summary is not None


def test_main_with_skip_analyst_still_persists_an_automatic_snapshot(db_path, monkeypatch):
    """The --skip-analyst branch never calls refresh_all -- it must still
    reach the same shared snapshot step as every other branch."""
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    module = _load_script_module()
    monkeypatch.setattr(module, "YFinanceProvider", _FakeProvider)
    monkeypatch.setattr(sys, "argv", ["refresh_supplemental_research.py", "--skip-analyst", "NVDA"])

    settings = load_settings()
    engine = make_engine(f"sqlite:///{db_path}")
    service = ResearchService(engine, settings)

    exit_code = module.main()

    assert exit_code == 0
    assert len(service.get_research_history("NVDA")) == 1
