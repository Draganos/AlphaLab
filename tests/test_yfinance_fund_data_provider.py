"""Offline tests for YFinanceProvider.get_fund_data. No network access --
every test replaces YFinanceProvider._ticker with a fake. Fixture shapes
mirror the real live payloads captured for FTEC/GDX during the PR #30
investigation."""

import pandas as pd
import pytest
import yfinance.exceptions as yf_exceptions

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.providers.yfinance_provider import YFinanceProvider

_FUND_OPERATIONS = pd.DataFrame(
    {
        "FTEC": [0.00084, 0.09, 691876.75],
        "Category Average": [0.009007, 0.582, 691876.75],
    },
    index=["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"],
)
_FUND_OPERATIONS.index.name = "Attributes"

_EQUITY_HOLDINGS = pd.DataFrame(
    {
        "FTEC": [0.0311, 0.09579, 0.13374, 0.03748, pd.NA, pd.NA],
        "Category Average": [pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA],
    },
    index=[
        "Price/Earnings", "Price/Book", "Price/Sales", "Price/Cashflow",
        "Median Market Cap", "3 Year Earnings Growth",
    ],
)
_EQUITY_HOLDINGS.index.name = "Average"

_TOP_HOLDINGS = pd.DataFrame(
    {"Name": ["NVIDIA Corp", "Apple Inc"], "Holding Percent": [0.177585, 0.158266]},
    index=pd.Index(["NVDA", "AAPL"], name="Symbol"),
)


class _FakeFundsData:
    def __init__(self, *, raises=None):
        self._raises = raises
        self.fund_overview = {
            "categoryName": "Technology", "family": "Fidelity Investments",
            "legalType": "Exchange Traded Fund",
        }
        self.asset_classes = {
            "cashPosition": 0.0006, "stockPosition": 0.9991, "bondPosition": 0.0,
            "preferredPosition": 0.0, "convertiblePosition": 0.0, "otherPosition": 0.0003,
        }
        self.sector_weightings = {"technology": 0.9933, "financial_services": 0.003}
        self.fund_operations = _FUND_OPERATIONS
        self.equity_holdings = _EQUITY_HOLDINGS
        self.top_holdings = _TOP_HOLDINGS
        self.description = "A technology sector fund."

    def _raise_if_configured(self):
        if self._raises is not None:
            raise self._raises

    def __getattribute__(self, name):
        # Any attribute access after construction can trigger yfinance's
        # lazy fetch -- mirror that by raising on first access to any of
        # the real data properties when configured to fail.
        if name in (
            "fund_overview", "asset_classes", "sector_weightings",
            "fund_operations", "equity_holdings", "top_holdings", "description",
        ):
            object.__getattribute__(self, "_raise_if_configured")()
        return object.__getattribute__(self, name)


class _FakeTicker:
    def __init__(self, funds_data):
        self._funds_data = funds_data

    @property
    def funds_data(self):
        return self._funds_data


def _provider_with_ticker(monkeypatch, fake_ticker):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: fake_ticker)
    return provider


def test_get_fund_data_returns_none_for_a_non_fund_ticker(monkeypatch):
    """Reproduces Ticker.funds_data on a plain equity: yfinance raises
    YFDataException on first sub-property access (funds_data itself is a
    lazy object and never raises on construction) -- a legitimate "not a
    fund" outcome, never a provider failure."""
    fake_funds = _FakeFundsData(raises=yf_exceptions.YFDataException("NVDA: No Fund data found."))
    provider = _provider_with_ticker(monkeypatch, _FakeTicker(fake_funds))
    assert provider.get_fund_data("NVDA") is None


def test_get_fund_data_raises_provider_error_classified_as_rate_limited(monkeypatch):
    fake_funds = _FakeFundsData(raises=yf_exceptions.YFRateLimitError())
    provider = _provider_with_ticker(monkeypatch, _FakeTicker(fake_funds))
    with pytest.raises(ProviderError) as exc_info:
        provider.get_fund_data("FTEC")
    assert exc_info.value.kind == ProviderErrorKind.RATE_LIMITED


def test_get_fund_data_extracts_all_fields_from_a_real_fund_shape(monkeypatch):
    provider = _provider_with_ticker(monkeypatch, _FakeTicker(_FakeFundsData()))
    raw = provider.get_fund_data("FTEC")
    assert raw["ticker"] == "FTEC"
    assert raw["category_name"] == "Technology"
    assert raw["fund_family"] == "Fidelity Investments"
    assert raw["stock_position"] == 0.9991
    assert raw["bond_position"] == 0.0
    assert raw["sector_weightings"] == {"technology": 0.9933, "financial_services": 0.003}
    assert raw["expense_ratio"] == 0.00084
    assert raw["category_avg_expense_ratio"] == pytest.approx(0.009007)
    assert raw["holdings_turnover"] == 0.09
    assert raw["total_net_assets"] == 691876.75
    assert raw["price_earnings"] == 0.0311
    assert raw["price_book"] == 0.09579
    assert raw["source"] == "YFinanceProvider"


def test_get_fund_data_top_holdings_preserves_order_and_weights(monkeypatch):
    provider = _provider_with_ticker(monkeypatch, _FakeTicker(_FakeFundsData()))
    raw = provider.get_fund_data("FTEC")
    assert raw["top_holdings"] == [
        {"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.177585},
        {"symbol": "AAPL", "name": "Apple Inc", "weight": 0.158266},
    ]


def test_get_fund_data_never_fabricates_unreliable_equity_holdings_fields(monkeypatch):
    """Median Market Cap / 3 Year Earnings Growth are <NA> in the real
    payload and are not extracted at all -- confirms the pd.NA cells don't
    silently become 0 or some other placeholder."""
    provider = _provider_with_ticker(monkeypatch, _FakeTicker(_FakeFundsData()))
    raw = provider.get_fund_data("FTEC")
    assert "median_market_cap" not in raw
    assert "three_year_earnings_growth" not in raw
