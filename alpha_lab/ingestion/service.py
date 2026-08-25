"""Idempotent provider-to-database ingestion."""

from datetime import date
import logging
import pandas as pd
from sqlalchemy import Engine

from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.session import session_scope
from alpha_lab.providers.base import MarketDataProvider

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, provider: MarketDataProvider, engine: Engine):
        self.provider, self.engine = provider, engine

    def ingest(self, ticker: str, start: date, end: date) -> None:
        symbol = ticker.upper().strip()
        info = self.provider.get_company_info(symbol)
        prices = self.provider.get_price_history(symbol, start, end)
        financials = self.provider.get_financials(symbol)
        with session_scope(self.engine) as session:
            session.merge(Security(**info))
            for index, row in prices.iterrows():
                values = {key: self._number(row.get(key)) for key in ["open", "high", "low", "close", "adjusted_close", "volume"]}
                existing = session.query(Price).filter_by(ticker=symbol, date=pd.Timestamp(index).date()).one_or_none()
                if existing:
                    for key, value in values.items(): setattr(existing, key, value)
                else:
                    session.add(Price(ticker=symbol, date=pd.Timestamp(index).date(), **values))
            for row in financials.to_dict("records"):
                period = pd.Timestamp(row.pop("period")).date()
                existing = session.query(Fundamental).filter_by(ticker=symbol, period=period).one_or_none()
                values = {key: (pd.Timestamp(value).date() if key == "publication_date" and value else self._number(value)) for key, value in row.items()}
                if existing:
                    for key, value in values.items(): setattr(existing, key, value)
                else:
                    session.add(Fundamental(ticker=symbol, period=period, **values))
        logger.info("ingestion_complete", extra={"ticker": symbol, "prices": len(prices), "fundamentals": len(financials)})

    @staticmethod
    def _number(value):
        return None if value is None or pd.isna(value) else float(value)
