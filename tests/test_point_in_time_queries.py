from datetime import date
from sqlalchemy.orm import Session
from alpha_lab.database import latest_estimates_as_of, latest_fundamentals_as_of
from alpha_lab.database.models import Estimate, Fundamental, Security


def test_future_estimates_and_restatements_do_not_leak(db_session: Session):
    db_session.add(Security(ticker="PIT"))
    db_session.add_all([
        Fundamental(ticker="PIT", period=date(2023, 12, 31), publication_date=date(2024, 2, 1),
                    eps=1, provider="test", observation_hash="v1"),
        Fundamental(ticker="PIT", period=date(2023, 12, 31), publication_date=date(2024, 5, 1),
                    eps=2, provider="test", observation_hash="v2"),
        Fundamental(ticker="PIT", period=date(2024, 3, 31), publication_date=None,
                    eps=99, provider="test", observation_hash="unknown"),
        Estimate(ticker="PIT", observation_date=date(2024, 2, 1), fiscal_period=date(2024, 12, 31),
                 consensus_eps=1, provider="test"),
        Estimate(ticker="PIT", observation_date=date(2024, 5, 1), fiscal_period=date(2024, 12, 31),
                 consensus_eps=2, provider="test"),
    ])
    db_session.flush()
    assert latest_fundamentals_as_of(db_session, "PIT", date(2024, 3, 1))[0].eps == 1
    assert latest_fundamentals_as_of(db_session, "PIT", date(2024, 6, 1))[0].eps == 2
    assert latest_estimates_as_of(db_session, "PIT", date(2024, 3, 1))[0].consensus_eps == 1
