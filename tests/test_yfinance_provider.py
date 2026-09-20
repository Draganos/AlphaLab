"""Offline tests for YFinanceProvider's failure handling and Ticker reuse.

Every test replaces YFinanceProvider._ticker with a fake -- no test here
ever talks to real Yahoo Finance.
"""
from datetime import date

import pandas as pd
import pytest
import yfinance.exceptions as yf_exceptions

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.providers.yfinance_provider import YFinanceProvider, _yahoo_symbol


class _FakeTicker:
    def __init__(self, *, info=None, history=None, income=None, cashflow=None, balance=None, raises=None):
        self._info = info or {}
        self._history = history if history is not None else pd.DataFrame()
        self.quarterly_income_stmt = income if income is not None else pd.DataFrame()
        self.quarterly_cashflow = cashflow if cashflow is not None else pd.DataFrame()
        self.quarterly_balance_sheet = balance if balance is not None else pd.DataFrame()
        self._raises = raises

    def get_info(self):
        if self._raises is not None:
            raise self._raises
        return self._info

    def history(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._history


def _provider_with_ticker(monkeypatch, fake_ticker):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: fake_ticker)
    return provider


def test_get_company_info_succeeds_with_a_normal_mocked_response(monkeypatch):
    fake = _FakeTicker(info={"longName": "Apple Inc.", "exchange": "NMS", "currency": "USD"})
    provider = _provider_with_ticker(monkeypatch, fake)
    info = provider.get_company_info("AAPL")
    assert info["company_name"] == "Apple Inc."
    assert info["currency"] == "USD"


def test_get_company_info_raises_provider_error_classified_as_rate_limited(monkeypatch):
    fake = _FakeTicker(raises=yf_exceptions.YFRateLimitError())
    provider = _provider_with_ticker(monkeypatch, fake)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_company_info("NVDA")
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
    assert "JSONDecodeError" not in str(excinfo.value)


def test_get_price_history_raises_provider_error_classified_as_network_unavailable(monkeypatch):
    import requests.exceptions

    fake = _FakeTicker(raises=requests.exceptions.ConnectionError("Failed to resolve host"))
    provider = _provider_with_ticker(monkeypatch, fake)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 2, 1))
    assert excinfo.value.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_get_financials_reuses_a_single_ticker_object_not_three(monkeypatch):
    """Regression guard for the original inefficiency: get_financials must
    not construct a new Ticker per statement."""
    construction_count = 0

    income = pd.DataFrame(
        {pd.Timestamp("2024-03-31"): {"Total Revenue": 100.0, "Diluted EPS": 1.0}}
    )

    def _ticker_factory(symbol):
        nonlocal construction_count
        construction_count += 1
        return _FakeTicker(income=income)

    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", _ticker_factory)
    provider.get_financials("AAPL")
    assert construction_count == 1


def test_get_financials_propagates_classified_error_from_any_of_the_three_statements(monkeypatch):
    # Simulate the balance-sheet property itself raising when accessed.
    class _FailingTicker(_FakeTicker):
        @property
        def quarterly_balance_sheet(self):
            raise yf_exceptions.YFRateLimitError()

        @quarterly_balance_sheet.setter
        def quarterly_balance_sheet(self, value):
            pass

    income = pd.DataFrame({pd.Timestamp("2024-03-31"): {"Total Revenue": 100.0}})
    failing = _FailingTicker(income=income)
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: failing)

    with pytest.raises(ProviderError) as excinfo:
        provider.get_financials("AAPL")
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED


# --- _yahoo_symbol: real-production finding -- Yahoo 404s AlphaLab's own
# canonical dot/dollar share-class notation forever, every refresh, until
# translated to the hyphen notation Yahoo actually recognizes -----------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("BRK.B", "BRK-B"),
        ("BRK.A", "BRK-A"),
        ("AGM.A", "AGM-A"),
        ("BF.A", "BF-A"),
        ("BF.B", "BF-B"),
        ("AHL$D", "AHL-PD"),
        ("ALL$B", "ALL-PB"),
        ("BAC$L", "BAC-PL"),
        ("DBRG$H", "DBRG-PH"),
        # No special notation: passed through unchanged.
        ("AAPL", "AAPL"),
        ("NVDA", "NVDA"),
    ],
)
def test_yahoo_symbol_translates_dot_and_dollar_share_class_notation(ticker, expected):
    """Verified live against real Yahoo Finance data (not guessed): the dot
    form 404s while the hyphen form resolves, and the dollar form 404s
    while hyphen-plus-P resolves, for every ticker reported stuck
    permanently stale in production."""
    assert _yahoo_symbol(ticker) == expected


def test_ticker_construction_uses_the_translated_yahoo_symbol_not_the_canonical_ticker(monkeypatch):
    """The canonical ticker (as stored/displayed everywhere else in
    AlphaLab) must never itself be rewritten -- only what actually goes
    out over the wire to Yahoo. Confirms this at the single choke point
    (_ticker) all provider methods share."""
    captured_symbols: list[str] = []

    class _FakeYFModule:
        @staticmethod
        def Ticker(symbol):
            captured_symbols.append(symbol)
            return _FakeTicker(info={"longName": "Berkshire Hathaway"})

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYFModule())
    provider = YFinanceProvider()

    info = provider.get_company_info("BRK.B")

    assert captured_symbols == ["BRK-B"]
    assert info["ticker"] == "BRK.B"  # the canonical ticker, unchanged
