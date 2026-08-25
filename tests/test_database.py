from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security


def test_database_crud():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="TEST", company_name="Test", country="US"))
        session.add(Price(ticker="TEST", date=date(2024, 1, 1), close=10, adjusted_close=10))
        session.commit()
        assert session.scalar(select(Security).where(Security.ticker == "TEST")).company_name == "Test"
