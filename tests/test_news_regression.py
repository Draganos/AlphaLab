"""Regression guard: the News Engine must have zero effect on every
existing AlphaLab scoring/ranking system, mirroring
tests/test_alignment_regression.py and tests/test_calibration_regression.py's
two-layer proof."""

from datetime import date, datetime
import math
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.news.service import NewsService
from alpha_lab.strategy import HistoricalScoringService


class _FakeNewsProvider:
    provider_name = "FakeNewsProvider"

    def get_news(self, ticker, since=None):
        return [
            {
                "title": "Fixture article for regression test",
                "url": "https://example.com/regression",
                "publisher": "Fixture Wire",
                "summary": "Nothing here should affect scoring.",
                "published_at": datetime(2024, 11, 1, 9, 0),
                "raw": {},
            }
        ]


def test_no_scoring_module_imports_the_news_code():
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                    "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                    "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "alpha_lab.news" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import News code: {offenders}"


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
            observation_hash="xyz-fundamental-news-v1",
        ))
        session.commit()


def test_historical_scoring_is_identical_with_and_without_news_articles():
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
    NewsService(engine_with).refresh(_FakeNewsProvider(), "XYZ")
    with_result = HistoricalScoringService(engine_with, settings).score_universe_as_of(
        date(2024, 11, 1), tickers=["XYZ"]
    )

    assert len(without_result) == len(with_result) == 1
    a, b = without_result[0], with_result[0]
    assert a.score == b.score
    assert _nan_safe_equal(a.raw_factors, b.raw_factors)
    assert _nan_safe_equal(a.percentile_factors, b.percentile_factors)
    assert _nan_safe_equal(a.category_scores, b.category_scores)
    assert a.eligible == b.eligible
    assert a.exclusion_reason == b.exclusion_reason


def _nan_safe_equal(left: dict, right: dict) -> bool:
    if left.keys() != right.keys():
        return False
    for key in left:
        a_value, b_value = left[key], right[key]
        if isinstance(a_value, float) and isinstance(b_value, float) and math.isnan(a_value) and math.isnan(b_value):
            continue
        if a_value != b_value:
            return False
    return True


def test_news_refresh_never_touches_price_or_fundamental_tables():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_fundamental_data(engine)
    with Session(engine) as session:
        price_count_before = session.query(Price).filter_by(ticker="XYZ").count()
        fundamental_count_before = session.query(Fundamental).count()

    NewsService(engine).refresh(_FakeNewsProvider(), "XYZ")

    with Session(engine) as session:
        assert session.query(Price).filter_by(ticker="XYZ").count() == price_count_before
        assert session.query(Fundamental).count() == fundamental_count_before
