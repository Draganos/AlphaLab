"""Phase 3 provider boundaries; credentials and networks are never required by core logic."""

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class SecurityUniverseProvider(ABC):
    @abstractmethod
    def get_securities(
        self, *, country: str = "US", exchanges: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def refresh_metadata(self, tickers: list[str]) -> list[dict[str, Any]]: ...


class FundamentalDataProvider(ABC):
    @abstractmethod
    def get_fundamentals(self, ticker: str) -> list[dict[str, Any]]: ...


class EstimateProvider(ABC):
    @abstractmethod
    def get_estimates(
        self, ticker: str, observation_date: date
    ) -> list[dict[str, Any]]: ...


class CompanyDocumentProvider(ABC):
    @abstractmethod
    def get_documents(
        self, ticker: str, since: date | None = None
    ) -> list[dict[str, Any]]: ...


class BusinessClassificationProvider(ABC):
    @abstractmethod
    def classify(self, ticker: str, description: str | None) -> dict[str, Any]: ...


class ResearchNewsProvider(ABC):
    @abstractmethod
    def get_news(
        self, ticker: str, since: date | None = None
    ) -> list[dict[str, Any]]: ...
