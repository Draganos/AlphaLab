from datetime import date
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.queries import latest_fundamentals_as_of
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider


class FakeProvider(MarketDataProvider):
    def get_company_info(self, ticker): return {"ticker": ticker, "company_name": "Fixture", "country": "US", "currency": "USD"}
    def get_price_history(self, ticker, start, end):
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))
    def get_financials(self, ticker):
        return pd.DataFrame([{"period": date(2023, 12, 31), "publication_date": None, "eps": 1.0}])


def test_ingestion_is_idempotent_and_preserves_unknown_publication_date():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(FakeProvider(), engine)
    service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Price)) == 1
        fundamental = session.scalar(select(Fundamental))
        assert fundamental.publication_date is None
        assert fundamental.provider == "FakeProvider"
        assert fundamental.currency == "USD"


class RevisingProvider(FakeProvider):
    def get_financials(self, ticker):
        return pd.DataFrame([
            {"period": date(2023, 12, 31), "publication_date": date(2024, 2, 1),
             "eps": 1.0, "source": "original filing"},
            {"period": date(2023, 12, 31), "publication_date": date(2024, 4, 1),
             "eps": 1.2, "source": "restated filing"},
        ])


def test_fundamental_revisions_are_append_only_idempotent_and_point_in_time():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(RevisingProvider(), engine)
    service.ingest("REV", date(2024, 1, 1), date(2024, 5, 1))
    service.ingest("REV", date(2024, 1, 1), date(2024, 5, 1))
    with Session(engine) as session:
        versions = session.scalars(select(Fundamental).order_by(Fundamental.publication_date)).all()
        assert len(versions) == 2
        assert [version.eps for version in versions] == [1.0, 1.2]
        before = latest_fundamentals_as_of(session, "REV", date(2024, 3, 1))
        after = latest_fundamentals_as_of(session, "REV", date(2024, 5, 1))
        assert len(before) == len(after) == 1
        assert before[0].eps == 1.0
        assert before[0].source == "original filing"
        assert after[0].eps == 1.2
        assert after[0].source == "restated filing"


def test_market_provider_cannot_replace_canonical_universe_exchange():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="ABC", exchange="NASDAQ"))
        session.commit()

    class ExchangeProvider(FakeProvider):
        def get_company_info(self, ticker):
            return {**super().get_company_info(ticker), "exchange": "NYQ"}

    IngestionService(ExchangeProvider(), engine).ingest(
        "ABC", date(2024, 1, 1), date(2024, 2, 1)
    )
    with Session(engine) as session:
        assert session.get(Security, "ABC").exchange == "NASDAQ"


def test_provider_failure_or_missing_refresh_does_not_erase_valid_price():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(FakeProvider(), engine)
    service.ingest("SAFE", date(2024, 1, 1), date(2024, 2, 1))

    class MissingProvider(FakeProvider):
        def get_price_history(self, ticker, start, end):
            return pd.DataFrame(
                {"close": [float("nan")], "adjusted_close": [float("inf")]},
                index=pd.to_datetime(["2024-01-01"]),
            )

    IngestionService(MissingProvider(), engine).ingest(
        "SAFE", date(2024, 1, 1), date(2024, 2, 1)
    )
    with Session(engine) as session:
        price = session.scalar(select(Price).where(Price.ticker == "SAFE"))
        assert price.close == price.adjusted_close == 10
