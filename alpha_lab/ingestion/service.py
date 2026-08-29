"""Idempotent provider-to-database ingestion."""

from datetime import UTC, date, datetime
import hashlib
import json
import logging
import pandas as pd
from sqlalchemy import Engine

from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.session import session_scope
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.ingestion.universe import _canonical_exchange

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, provider: MarketDataProvider, engine: Engine):
        self.provider, self.engine = provider, engine

    def ingest(self, ticker: str, start: date, end: date) -> None:
        symbol = ticker.upper().strip()
        info = self.provider.get_company_info(symbol)
        info["exchange"] = _canonical_exchange(info.get("exchange"))
        prices = self.provider.get_price_history(symbol, start, end)
        financials = self.provider.get_financials(symbol)
        provider_name = self.provider.provider_name
        currency = info.get("currency")
        with session_scope(self.engine) as session:
            security = session.get(Security, symbol)
            if security is None:
                security = Security(**info)
                session.add(security)
            else:
                for key, value in info.items():
                    if key != "ticker" and value is not None:
                        if key == "exchange" and security.exchange in {
                            "NASDAQ",
                            "NYSE",
                        }:
                            continue
                        setattr(security, key, value)
            security.metadata_updated_at = datetime.now(UTC)
            for index, row in prices.iterrows():
                values = {
                    key: self._number(row.get(key))
                    for key in [
                        "open",
                        "high",
                        "low",
                        "close",
                        "adjusted_close",
                        "volume",
                    ]
                }
                values.update(
                    currency=currency, provider=provider_name, source=row.get("source")
                )
                existing = (
                    session.query(Price)
                    .filter_by(ticker=symbol, date=pd.Timestamp(index).date())
                    .one_or_none()
                )
                if existing:
                    for key, value in values.items():
                        setattr(existing, key, value)
                else:
                    session.add(
                        Price(ticker=symbol, date=pd.Timestamp(index).date(), **values)
                    )
            for row in financials.to_dict("records"):
                period = pd.Timestamp(row.pop("period")).date()
                values = {
                    key: (
                        self._date(value)
                        if key == "publication_date"
                        else self._number(value)
                    )
                    for key, value in row.items()
                    if key != "source"
                }
                source = row.get("source")
                values.update(
                    currency=currency,
                    provider=provider_name,
                    source=None if source is None or pd.isna(source) else str(source),
                )
                observation_hash = self._fundamental_hash(symbol, period, values)
                existing = (
                    session.query(Fundamental.id)
                    .filter_by(observation_hash=observation_hash)
                    .one_or_none()
                )
                if existing is None:
                    session.add(
                        Fundamental(
                            ticker=symbol,
                            period=period,
                            observation_hash=observation_hash,
                            **values,
                        )
                    )
        logger.info(
            "ingestion_complete",
            extra={
                "ticker": symbol,
                "prices": len(prices),
                "fundamentals": len(financials),
            },
        )

    @staticmethod
    def _number(value):
        return None if value is None or pd.isna(value) else float(value)

    @staticmethod
    def _date(value):
        return None if value is None or pd.isna(value) else pd.Timestamp(value).date()

    @staticmethod
    def _fundamental_hash(ticker: str, period: date, values: dict) -> str:
        """Identify an exact provider observation while excluding ingestion time."""
        canonical = {"ticker": ticker, "period": period.isoformat(), **values}
        payload = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()
