"""Regression test for the Phase 2D-diagnosed blank-cell bug in
app/dashboard/main.py's Stock Screener: Revision/Valuation/Shareholder Return/
AI Rating were previously hardcoded to `None` regardless of actual evidence
(alpha_lab.strategy.historical.HistoricalScoringService._categories never
computed those four categories). Phase 2E replaced that data source with the
same canonical MarketScreenerService/ScreenRecord/apply_screen pipeline
app/dashboard/pages/3_Market_Screener.py already uses.

This test never touches HistoricalScoringService and never asserts on
Streamlit's rendered DOM/Glide table (see Phase 2D audit: the bug was a
Python-level data-construction issue, not a rendering defect) -- it imports
main.py directly (safe: every Streamlit call it makes degrades to a no-op
default in "bare mode", verified separately) and inspects the `screen`
DataFrame the module builds at import time, exactly as app/dashboard/main.py
computes it for real.
"""

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.screener import LiveResearchRecord

_MAIN_PATH = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"

_FULL_CATEGORIES = {
    "earnings_growth": 55.0,
    "analyst_revisions": 61.0,
    "business_quality": 72.0,
    "valuation": 48.0,
    "momentum": 80.0,
    "financial_strength": 66.0,
    "ai_research": 70.0,
    "shareholder_return": 33.0,
}


def _record(*, category_scores: dict, overall_score: float | None) -> LiveResearchRecord:
    return LiveResearchRecord(
        ticker="AAPL",
        company="Apple Inc.",
        price=175.9,
        market_cap=2_700_000_000_000,
        country="US",
        exchange="NASDAQ",
        sector="Technology",
        industry="Consumer Electronics",
        asset_type="EQUITY",
        ethical_status="PASS",
        data_quality_status="valid",
        overall_score=overall_score,
        category_scores=category_scores,
        category_coverage={name: (1.0 if value is not None else 0.0) for name, value in category_scores.items()},
        raw_metrics={"debt_ebitda": 1.2},
        percentile_metrics={},
        overall_live_coverage=0.9,
        quantitative_coverage=0.9,
        ai_coverage=1.0,
        historical_coverage=0.9,
        confidence="High",
        provenance={},
        last_refreshed=None,
        configuration_hash="fixture-phase2e",
        evaluation_date=date(2026, 1, 1),
    )


def _import_main(db_path, monkeypatch):
    """Load app/dashboard/main.py as a fresh module against `db_path`.

    A distinct module name per call avoids sys.modules caching a stale
    `screen` DataFrame from an earlier test's database."""
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    spec = importlib.util.spec_from_file_location(
        f"dashboard_main_under_test_{db_path.name}", _MAIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded_engine(tmp_path):
    def _seed(name: str, *, category_scores: dict, overall_score: float | None = 63.5):
        db_path = tmp_path / name
        engine = make_engine(f"sqlite:///{db_path}")
        create_schema(engine)
        Phase3Repository(engine).save_current_research(
            [_record(category_scores=category_scores, overall_score=overall_score)]
        )
        engine.dispose()
        return db_path

    return _seed


def test_screener_no_longer_hardcodes_revision_valuation_shareholder_ai_to_none(
    seeded_engine, monkeypatch
):
    """The original bug: these four columns were `None` unconditionally. With
    the canonical pipeline and a fixture that actually has evidence for them,
    they must now carry the real computed values."""
    db_path = seeded_engine("full.db", category_scores=_FULL_CATEGORIES)
    module = _import_main(db_path, monkeypatch)
    try:
        assert not module.screen.empty
        row = module.screen.set_index("Ticker").loc["AAPL"]
        assert row["Revisions"] == 61.0
        assert row["Valuation"] == 48.0
        assert row["Shareholder Return"] == 33.0
        assert row["AI Rating"] == 70.0
        # And the columns that already worked in the legacy page still work.
        assert row["Growth"] == 55.0
        assert row["Quality"] == 72.0
        assert row["Momentum"] == 80.0
        assert row["Financial Strength"] == 66.0
        assert row["Overall Rating"] == 63.5
        assert row["Data Quality"] == "valid"
        assert row["Sharia Status"] == "PASS"
        assert row["Company"] == "Apple Inc."
        assert row["Price"] == 175.9
        assert row["Market Cap"] == 2_700_000_000_000
        assert row["Sector"] == "Technology"
        assert row["Industry"] == "Consumer Electronics"
    finally:
        module.engine.dispose()


def test_screener_leaves_genuinely_unavailable_categories_as_none_not_zero(
    seeded_engine, monkeypatch
):
    """A category with no evidence must stay unavailable (None/NaN) -- never
    coerced to 0, and never fabricated as if it were computed."""
    missing = dict(_FULL_CATEGORIES)
    for name in ("analyst_revisions", "valuation", "ai_research", "shareholder_return"):
        missing[name] = None
    db_path = seeded_engine("missing.db", category_scores=missing, overall_score=75.0)
    module = _import_main(db_path, monkeypatch)
    try:
        row = module.screen.set_index("Ticker").loc["AAPL"]
        for column in ("Revisions", "Valuation", "Shareholder Return", "AI Rating"):
            assert pd.isna(row[column]), f"{column} should be unavailable, got {row[column]!r}"
            assert row[column] != 0
        # Categories with real evidence must be unaffected by the others being absent.
        assert row["Growth"] == 55.0
        assert row["Quality"] == 72.0
    finally:
        module.engine.dispose()


def test_screener_shows_no_data_state_without_fabricating_rows_when_build_is_absent(
    tmp_path, monkeypatch
):
    """No persisted current research build -> the explicit empty state, never
    an empty-but-silently-rendered table and never a network/rebuild call."""
    db_path = tmp_path / "empty.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    engine.dispose()

    module = _import_main(db_path, monkeypatch)
    try:
        assert module.screen.empty
    finally:
        module.engine.dispose()


def test_screener_dataframe_is_populated_via_market_screener_services_canonical_read(
    monkeypatch, tmp_path
):
    """Positive architectural invariant: `screen` must come from
    MarketScreenerService.read_current_research() specifically -- not merely
    "some code path that isn't HistoricalScoringService", which a future
    duplicated/parallel scoring implementation could still satisfy.

    Proven dynamically: patch read_current_research itself to return a
    distinctive sentinel record unreachable by any other path (no matching
    row exists in the database at all), then assert `screen` is built
    exactly from what that one method returned."""
    from alpha_lab.screener.service import MarketScreenerService

    sentinel = _record(category_scores=_FULL_CATEGORIES, overall_score=91.0).model_copy(
        update={"ticker": "ZZZZ", "company": "Sentinel Research Corp"}
    )
    monkeypatch.setattr(
        MarketScreenerService, "read_current_research", lambda self: [sentinel]
    )

    # The patched method is the sole source build_screener() can read from;
    # main.py's own create_schema(engine) at import time only needs *a*
    # valid, empty database file to bootstrap against.
    db_path = tmp_path / "patched.db"
    module = _import_main(db_path, monkeypatch)
    try:
        assert list(module.screen["Ticker"]) == ["ZZZZ"]
        row = module.screen.set_index("Ticker").loc["ZZZZ"]
        assert row["Company"] == "Sentinel Research Corp"
        assert row["Overall Rating"] == 91.0
        assert row["Revisions"] == 61.0
        assert row["Valuation"] == 48.0
        assert row["Shareholder Return"] == 33.0
        assert row["AI Rating"] == 70.0
    finally:
        module.engine.dispose()


def test_screener_never_imports_or_calls_historical_scoring_service():
    """Cheap static guard, kept alongside the dynamic test above as a fast
    fail-early signal: the fixed Stock Screener section must not import or
    instantiate the legacy point-in-time/backtest scoring engine at all (a
    reference in an explanatory comment/docstring is fine)."""
    text = _MAIN_PATH.read_text()
    assert "import HistoricalScoringService" not in text
    assert "HistoricalScoringService(" not in text
