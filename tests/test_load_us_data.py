"""Deterministic tests for scripts/load_us_data.py's per-ticker failure
handling, summary reporting, and exit-code semantics.

Loads the script as a module (it is a script, not part of the alpha_lab
package) so its helper functions can be exercised directly, without ever
calling a real provider.
"""
from datetime import date
import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "load_us_data.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("load_us_data_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def loader_module():
    return _load_script_module()


class _FakeProvider(MarketDataProvider):
    """Succeeds for every ticker except those named in `failing`."""

    def __init__(self, failing: dict[str, ProviderError] | None = None):
        self.failing = failing or {}

    def get_company_info(self, ticker):
        if ticker in self.failing:
            raise self.failing[ticker]
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        if ticker in self.failing:
            raise self.failing[ticker]
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))

    def get_financials(self, ticker):
        return pd.DataFrame()


def test_ingest_universe_continues_past_a_single_ticker_failure(loader_module):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    failing = {"NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "YFinanceProvider", "Yahoo Finance rate-limited the request")}
    service = IngestionService(_FakeProvider(failing), engine)

    succeeded = loader_module._ingest_universe(service, ["AAPL", "NVDA", "MSFT"], years=1)

    assert succeeded is False
    with Session(engine) as session:
        tickers = set(session.scalars(select(Security.ticker)))
    assert tickers == {"AAPL", "MSFT"}


def test_ingest_universe_returns_true_only_when_every_ticker_succeeds(loader_module):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(_FakeProvider(), engine)

    assert loader_module._ingest_universe(service, ["AAPL", "MSFT"], years=1) is True


def test_ingest_universe_summary_categorizes_failures_by_kind(loader_module, capsys):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    failing = {
        "NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "YFinanceProvider", "Yahoo Finance rate-limited the request"),
        "XYZ": ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "YFinanceProvider", "DNS resolution failure"),
    }
    service = IngestionService(_FakeProvider(failing), engine)

    loader_module._ingest_universe(service, ["AAPL", "NVDA", "XYZ"], years=1)
    output = capsys.readouterr().out

    assert "Succeeded: 1" in output
    assert "Failed: 2" in output
    assert "Rate Limited" in output
    assert "NVDA" in output
    assert "Network Unavailable" in output
    assert "XYZ" in output


def test_ingest_universe_does_not_erase_previously_ingested_data_on_later_failure(loader_module):
    """A rate-limited refresh for one ticker must never clear out data that
    a prior successful run already persisted for it."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(_FakeProvider(), engine)
    loader_module._ingest_universe(service, ["NVDA"], years=1)

    with Session(engine) as session:
        before = session.scalar(select(Price).where(Price.ticker == "NVDA"))
    assert before is not None

    failing_provider = _FakeProvider(
        {"NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "YFinanceProvider", "rate limited")}
    )
    failing_service = IngestionService(failing_provider, engine)
    loader_module._ingest_universe(failing_service, ["NVDA"], years=1)

    with Session(engine) as session:
        after = session.scalar(select(Price).where(Price.ticker == "NVDA"))
        count = session.scalar(select(func.count()).select_from(Price).where(Price.ticker == "NVDA"))
    assert after is not None
    assert after.close == before.close
    assert count == 1
