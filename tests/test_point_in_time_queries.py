from datetime import date, datetime
from sqlalchemy.orm import Session
from alpha_lab.database import (
    latest_estimates_as_of,
    latest_fundamentals_as_of,
    latest_price_per_date,
)
from alpha_lab.database.models import Estimate, Fundamental, Price, Security


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


def test_price_revision_never_leaks_into_an_earlier_as_of(db_session: Session):
    """Mirrors FXRateService's own PIT-leak regression exactly, for Price:
    a later revision of an already-stored bar must never become visible to
    a historical as_of that predates the revision."""
    db_session.add(Security(ticker="PXR"))
    db_session.add(Price(
        ticker="PXR", date=date(2024, 1, 5), close=10.0,
        ingested_at=datetime(2024, 1, 5),
    ))
    db_session.flush()
    rows = latest_price_per_date(
        db_session, Price.ticker == "PXR", Price.date <= date(2024, 1, 5),
        Price.ingested_at <= datetime(2024, 1, 5, 23, 59, 59),
    )
    assert [row.close for row in rows] == [10.0]

    db_session.add(Price(
        ticker="PXR", date=date(2024, 1, 5), close=11.0,
        ingested_at=datetime(2024, 1, 10),
    ))
    db_session.flush()

    between_the_two_revisions = datetime(2024, 1, 7, 23, 59, 59)
    still_original = latest_price_per_date(
        db_session, Price.ticker == "PXR", Price.date <= date(2024, 1, 5),
        Price.ingested_at <= between_the_two_revisions,
    )
    assert [row.close for row in still_original] == [10.0]

    after_the_revision = latest_price_per_date(
        db_session, Price.ticker == "PXR", Price.date <= date(2024, 1, 5),
        Price.ingested_at <= datetime(2024, 1, 10, 23, 59, 59),
    )
    assert [row.close for row in after_the_revision] == [11.0]


def test_latest_price_per_date_partitions_by_ticker_not_just_date(db_session: Session):
    """A caller that queries across multiple tickers at once (no
    Price.ticker condition) must still get one row per ticker per date --
    two different tickers sharing a date must never collapse into each
    other via the ranking partition."""
    db_session.add(Security(ticker="AAA"))
    db_session.add(Security(ticker="BBB"))
    db_session.add(Price(ticker="AAA", date=date(2024, 1, 5), close=10.0))
    db_session.add(Price(ticker="BBB", date=date(2024, 1, 5), close=20.0))
    db_session.flush()
    rows = latest_price_per_date(db_session, Price.date == date(2024, 1, 5))
    assert {(row.ticker, row.close) for row in rows} == {("AAA", 10.0), ("BBB", 20.0)}
