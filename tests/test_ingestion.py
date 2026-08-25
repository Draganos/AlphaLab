from datetime import date
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider


class FakeProvider(MarketDataProvider):
    def get_company_info(self, ticker): return {"ticker": ticker, "company_name": "Fixture", "country": "US"}
    def get_price_history(self, ticker, start, end):
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))
    def get_financials(self, ticker):
        return pd.DataFrame([{"period": date(2023, 12, 31), "publication_date": None, "eps": 1.0}])


def test_ingestion_is_idempotent_and_preserves_unknown_publication_date():
    engine = make_engine("sqlite:///:memory:"); create_schema(engine)
    service = IngestionService(FakeProvider(), engine)
    service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1)); service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Price)) == 1
        assert session.scalar(select(Fundamental)).publication_date is None
