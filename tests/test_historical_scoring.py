from datetime import date, timedelta
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.strategy import HistoricalScoringService


def test_historical_scoring_ignores_future_prices_and_fundamentals():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    evaluation = date(2024, 12, 31)
    with Session(engine) as session:
        session.add(Security(ticker="PIT", sector="Test"))
        start = evaluation - timedelta(days=420)
        for offset in range(300):
            day = start + timedelta(days=offset)
            session.add(Price(ticker="PIT", date=day, open=10 + offset, close=10 + offset,
                              adjusted_close=10 + offset, provider="test"))
        session.add(Price(ticker="PIT", date=date(2025, 1, 2), open=1_000_000, close=1_000_000,
                          adjusted_close=1_000_000, provider="test"))
        for index in range(5):
            session.add(Fundamental(ticker="PIT", period=date(2023 + index // 4, (index % 4) * 3 + 3, 28),
                publication_date=date(2023 + index // 4, (index % 4) * 3 + 3, 28) + timedelta(days=30),
                eps=1 + index, revenue=100 + index, provider="test", observation_hash=f"known-{index}"))
        session.add(Fundamental(ticker="PIT", period=date(2024, 3, 28), publication_date=date(2025, 2, 1),
                                eps=999, revenue=999, provider="test", observation_hash="future"))
        session.commit()
    try:
        result = HistoricalScoringService(engine, load_settings()).score_universe_as_of(
            evaluation, ["PIT"], min_score=0, minimum_coverage=0)[0]
        assert result.raw_factors["last_price"] < 1_000_000
        assert result.raw_factors["eps_yoy_growth"] == 4
        assert result.evaluation_date == evaluation
        assert result.config_hash
    finally:
        engine.dispose()
