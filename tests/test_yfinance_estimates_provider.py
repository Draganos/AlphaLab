"""Offline tests for YFinanceProvider.get_estimates: consensus EPS/revenue
snapshot construction, fiscal-period-date derivation, and failure handling.
No network access -- every test replaces YFinanceProvider._ticker with a fake."""

from datetime import date

import pandas as pd
import pytest
import yfinance.exceptions as yf_exceptions

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.providers.yfinance_provider import (
    YFinanceProvider,
    _fiscal_period_for,
)

# NVDA-shaped fixture: mostRecentQuarter/nextFiscalYearEnd are the same real
# anchors captured live during the Phase 2H investigation.
_NVDA_INFO = {
    "mostRecentQuarter": 1785024000,  # 2026-07-26
    "nextFiscalYearEnd": 1800835200,  # 2027-01-25
}

_EARNINGS_ESTIMATE = pd.DataFrame(
    {
        "avg": [2.47269, 2.74328, 9.30456, 15.56753],
        "low": [2.34, 2.56, 9.01, 9.80],
        "high": [2.70, 3.117, 10.10, 18.746],
        "numberOfAnalysts": [42, 40, 49, 53],
    },
    index=pd.Index(["0q", "+1q", "0y", "+1y"], name="period"),
)

_REVENUE_ESTIMATE = pd.DataFrame(
    {"avg": [108988683310, 124266648840, 411292753830, 677919776510]},
    index=pd.Index(["0q", "+1q", "0y", "+1y"], name="period"),
)


class _FakeTicker:
    def __init__(self, *, info=None, earnings=None, revenue=None, raises=None):
        self._info = info if info is not None else {}
        self._earnings = earnings if earnings is not None else pd.DataFrame()
        self._revenue = revenue if revenue is not None else pd.DataFrame()
        self._raises = raises

    def get_info(self):
        if self._raises is not None:
            raise self._raises
        return self._info

    def get_earnings_estimate(self):
        if self._raises is not None:
            raise self._raises
        return self._earnings

    def get_revenue_estimate(self):
        if self._raises is not None:
            raise self._raises
        return self._revenue


def _provider_with_ticker(monkeypatch, fake_ticker):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: fake_ticker)
    return provider


# --- fiscal-period derivation (pure) ----------------------------------------


def test_fiscal_period_for_0y_uses_the_anchor_directly():
    anchors = {"next_fiscal_year_end": date(2027, 1, 25)}
    assert _fiscal_period_for("0y", anchors) == date(2027, 1, 25)


def test_fiscal_period_for_plus_1y_is_exactly_12_months_later():
    anchors = {"next_fiscal_year_end": date(2027, 1, 25)}
    assert _fiscal_period_for("+1y", anchors) == date(2028, 1, 25)


def test_fiscal_period_for_quarterly_labels_is_always_none():
    """Deliberate: quarterly consensus periods are never captured because
    their exact end date cannot be derived precisely (see
    YFinanceProvider.get_estimates's docstring) -- proven here for a
    calendar-year-end company where naive month-arithmetic would silently
    land a day off the true quarter-end."""
    anchors = {"next_fiscal_year_end": date(2026, 12, 31)}
    assert _fiscal_period_for("0q", anchors) is None
    assert _fiscal_period_for("+1q", anchors) is None


def test_fiscal_period_for_missing_anchor_is_none_not_fabricated():
    assert _fiscal_period_for("0y", {"next_fiscal_year_end": None}) is None
    assert _fiscal_period_for("+1y", {"next_fiscal_year_end": None}) is None


# --- get_estimates: end to end -----------------------------------------------


def test_get_estimates_returns_only_the_two_precise_annual_observations(monkeypatch):
    fake = _FakeTicker(info=_NVDA_INFO, earnings=_EARNINGS_ESTIMATE, revenue=_REVENUE_ESTIMATE)
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = provider.get_estimates("NVDA", date(2026, 9, 14))
    periods = {item["fiscal_period"] for item in observations}
    assert periods == {date(2027, 1, 25), date(2028, 1, 25)}
    assert len(observations) == 2


def test_get_estimates_carries_correct_values_and_provenance_free_fields(monkeypatch):
    fake = _FakeTicker(info=_NVDA_INFO, earnings=_EARNINGS_ESTIMATE, revenue=_REVENUE_ESTIMATE)
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = {
        item["fiscal_period"]: item for item in provider.get_estimates("NVDA", date(2026, 9, 14))
    }
    current_year = observations[date(2027, 1, 25)]
    assert current_year["consensus_eps"] == 9.30456
    assert current_year["consensus_revenue"] == 411292753830
    assert current_year["analyst_count"] == 49
    assert current_year["estimate_dispersion"] == pytest.approx(10.10 - 9.01)


def test_get_estimates_returns_empty_list_when_provider_has_no_coverage(monkeypatch):
    """The confirmed live ETF behaviour: yfinance returns an empty frame,
    not an error -- "no analyst estimate coverage for this instrument type",
    never fabricated as a failure or as zero values."""
    fake = _FakeTicker(info={}, earnings=pd.DataFrame(), revenue=pd.DataFrame())
    provider = _provider_with_ticker(monkeypatch, fake)
    assert provider.get_estimates("FTEC", date(2026, 9, 14)) == []


def test_get_estimates_drops_periods_it_cannot_anchor_even_with_earnings_data(monkeypatch):
    """Earnings estimate rows exist, but get_info() never reported
    nextFiscalYearEnd -- every period must be dropped rather than assigned a
    fabricated date."""
    fake = _FakeTicker(info={}, earnings=_EARNINGS_ESTIMATE, revenue=_REVENUE_ESTIMATE)
    provider = _provider_with_ticker(monkeypatch, fake)
    assert provider.get_estimates("NVDA", date(2026, 9, 14)) == []


def test_get_estimates_skips_a_period_with_no_consensus_eps(monkeypatch):
    earnings = _EARNINGS_ESTIMATE.copy()
    earnings.loc["0y", "avg"] = float("nan")
    fake = _FakeTicker(info=_NVDA_INFO, earnings=earnings, revenue=_REVENUE_ESTIMATE)
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = provider.get_estimates("NVDA", date(2026, 9, 14))
    periods = {item["fiscal_period"] for item in observations}
    assert periods == {date(2028, 1, 25)}


def test_get_estimates_missing_revenue_estimate_leaves_consensus_revenue_none(monkeypatch):
    fake = _FakeTicker(info=_NVDA_INFO, earnings=_EARNINGS_ESTIMATE, revenue=pd.DataFrame())
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = {
        item["fiscal_period"]: item for item in provider.get_estimates("NVDA", date(2026, 9, 14))
    }
    assert observations[date(2027, 1, 25)]["consensus_revenue"] is None
    assert observations[date(2027, 1, 25)]["consensus_eps"] == 9.30456


def test_get_estimates_missing_low_high_leaves_dispersion_none_not_zero(monkeypatch):
    earnings = _EARNINGS_ESTIMATE.copy()
    earnings.loc["0y", "low"] = float("nan")
    fake = _FakeTicker(info=_NVDA_INFO, earnings=earnings, revenue=_REVENUE_ESTIMATE)
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = {
        item["fiscal_period"]: item for item in provider.get_estimates("NVDA", date(2026, 9, 14))
    }
    assert observations[date(2027, 1, 25)]["estimate_dispersion"] is None


def test_get_estimates_propagates_classified_provider_error_on_earnings_fetch_failure(monkeypatch):
    fake = _FakeTicker(raises=yf_exceptions.YFRateLimitError())
    provider = _provider_with_ticker(monkeypatch, fake)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_estimates("NVDA", date(2026, 9, 14))
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
