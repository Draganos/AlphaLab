from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from alpha_lab.database.models import Estimate, Fundamental, Price, Security
from alpha_lab.database import create_schema, make_engine
from sqlalchemy import inspect


def test_database_crud(db_session: Session):
    db_session.add(Security(ticker="TEST", company_name="Test", country="US", currency="USD"))
    db_session.add(Price(ticker="TEST", date=date(2024, 1, 1), close=10, adjusted_close=10,
                         provider="test-fixture", currency="USD", source="unit test"))
    db_session.commit()
    price = db_session.scalar(select(Price).where(Price.ticker == "TEST"))
    assert price.close == 10
    assert price.provider == "test-fixture"
    assert price.ingested_at is not None


def test_schema_initialization_is_idempotent():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    create_schema(engine)
    assert "provider" in {column["name"] for column in inspect(engine).get_columns("prices")}


def test_point_in_time_fields_are_separate(db_session: Session):
    db_session.add(Security(ticker="ASOF", currency="USD"))
    db_session.add(Fundamental(ticker="ASOF", period=date(2024, 3, 31),
                              publication_date=date(2024, 5, 10), provider="fixture"))
    db_session.add(Estimate(ticker="ASOF", observation_date=date(2024, 4, 1),
                            fiscal_period=date(2024, 6, 30), consensus_eps=1.5, provider="fixture"))
    db_session.flush()
    fundamental = db_session.scalar(select(Fundamental))
    estimate = db_session.scalar(select(Estimate))
    assert fundamental.period != fundamental.publication_date
    assert estimate.observation_date != estimate.fiscal_period
