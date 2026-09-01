"""Deterministic offline fixture provider; data is explicitly synthetic."""

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from alpha_lab.providers.base import MarketDataProvider


class SyntheticFixtureProvider(MarketDataProvider):
    """Stable test/demo observations, never presented as real market data."""

    @property
    def provider_name(self) -> str:
        return "synthetic-fixture-v1"

    def get_company_info(self, ticker: str) -> dict[str, Any]:
        return {"ticker": ticker, "company_name": f"Synthetic {ticker}", "exchange": "FIXTURE",
                "country": "TEST", "sector": "Fixture", "currency": "USD", "asset_type": "EQUITY"}

    def get_price_history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        frame = pd.read_csv(self._fixture_path("synthetic_prices.csv"), parse_dates=["date"]).set_index("date")
        return frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))].drop(columns="currency")

    def get_financials(self, ticker: str) -> pd.DataFrame:
        frame = pd.read_csv(self._fixture_path("synthetic_fundamentals.csv"), parse_dates=["period", "publication_date"])
        frame["period"] = frame["period"].dt.date
        frame["publication_date"] = frame["publication_date"].dt.date
        return frame.drop(columns="currency")

    @staticmethod
    def _fixture_path(filename: str) -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "fixtures" / filename
