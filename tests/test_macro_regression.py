"""Regression guard: AlphaLab Macro Regime must have zero effect on every
existing scoring/ranking system, mirroring
tests/test_calibration_regression.py's two-layer proof for Donatien."""

from datetime import date
import math
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.macro.service import MacroRegimeService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError
from alpha_lab.strategy import HistoricalScoringService
import pandas as pd


class _FakeMacroProvider(MarketDataProvider):
    provider_name = "FakeMacroProvider"

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"Fixture {ticker}", "asset_type": "INDEX", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        idx = pd.date_range(end=pd.Timestamp(end), periods=60)
        return pd.DataFrame({"close": [50.0] * 60, "high": [50.0] * 60, "low": [50.0] * 60}, index=idx)

    def get_financials(self, ticker):
        return pd.DataFrame()


def test_no_scoring_module_imports_the_macro_regime_code():
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                    "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                    "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "alpha_lab.macro" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import macro regime code: {offenders}"


def _seed_fundamental_data(engine):
    with Session(engine) as session:
        session.add(Security(ticker="XYZ", country="US", currency="USD", sector="Technology"))
        for i in range(300):
            session.add(Price(
                ticker="XYZ", date=date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + i),
                close=100 + i * 0.1, high=101 + i * 0.1, low=99 + i * 0.1,
                provider="fixture", currency="USD", source="test",
            ))
        session.add(Fundamental(
            ticker="XYZ", period=date(2023, 12, 31), publication_date=date(2024, 2, 1),
            revenue=1_000_000, net_income=100_000, eps=1.0, total_equity=500_000,
            total_debt=100_000, cash=200_000, provider="fixture",
            observation_hash="xyz-fundamental-macro-v1",
        ))
        session.commit()


def test_historical_scoring_is_identical_with_and_without_a_macro_assessment():
    settings = load_settings()

    engine_without = make_engine("sqlite:///:memory:")
    create_schema(engine_without)
    _seed_fundamental_data(engine_without)
    without_result = HistoricalScoringService(engine_without, settings).score_universe_as_of(
        date(2024, 11, 1), tickers=["XYZ"]
    )

    engine_with = make_engine("sqlite:///:memory:")
    create_schema(engine_with)
    _seed_fundamental_data(engine_with)
    MacroRegimeService(engine_with).refresh(_FakeMacroProvider(), as_of=date(2024, 11, 1))
    with_result = HistoricalScoringService(engine_with, settings).score_universe_as_of(
        date(2024, 11, 1), tickers=["XYZ"]
    )

    assert len(without_result) == len(with_result) == 1
    a, b = without_result[0], with_result[0]
    assert a.score == b.score
    assert _nan_safe_equal(a.raw_factors, b.raw_factors)
    assert a.eligible == b.eligible


def _nan_safe_equal(left: dict, right: dict) -> bool:
    """Regular dict equality, except NaN == NaN (unlike Python's default) --
    both sides come from the same deterministic factor math and are
    expected to be NaN in exactly the same places when data is sparse."""
    if left.keys() != right.keys():
        return False
    for key in left:
        a_value, b_value = left[key], right[key]
        if isinstance(a_value, float) and isinstance(b_value, float) and math.isnan(a_value) and math.isnan(b_value):
            continue
        if a_value != b_value:
            return False
    return True


def test_macro_refresh_never_touches_the_price_table_for_the_scored_security():
    """The macro proxy ingestion (^VIX etc.) must never write Price rows
    for an unrelated ticker -- confirms the two ingestion paths stay
    scoped to their own tickers."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_fundamental_data(engine)
    with Session(engine) as session:
        xyz_price_count_before = session.query(Price).filter_by(ticker="XYZ").count()

    MacroRegimeService(engine).refresh(_FakeMacroProvider(), as_of=date(2024, 11, 1))

    with Session(engine) as session:
        xyz_price_count_after = session.query(Price).filter_by(ticker="XYZ").count()
        macro_tickers_present = {
            row.ticker for row in session.query(Price.ticker).distinct()
        } - {"XYZ"}
    assert xyz_price_count_after == xyz_price_count_before
    assert macro_tickers_present  # the macro proxy tickers did get ingested, just not XYZ's rows
