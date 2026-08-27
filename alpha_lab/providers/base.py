"""Replaceable market-data boundary."""

from abc import ABC, abstractmethod
from datetime import date
from typing import Any
import pandas as pd


class MarketDataProvider(ABC):
    @property
    def provider_name(self) -> str:
        """Stable provenance identifier stored with every observation."""
        return type(self).__name__

    @abstractmethod
    def get_price_history(self, ticker: str, start: date, end: date) -> pd.DataFrame: ...

    @abstractmethod
    def get_company_info(self, ticker: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_financials(self, ticker: str) -> pd.DataFrame: ...

    def get_earnings(self, ticker: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_analyst_estimates(self, ticker: str) -> pd.DataFrame:
        # yfinance does not provide reliable point-in-time estimate history.
        return pd.DataFrame()
