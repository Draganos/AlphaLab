"""Offline tests for YFinanceProvider.get_analyst_rating_changes and
.get_estimate_revision_trend. No network access -- every test replaces
YFinanceProvider._ticker with a fake. Fixture shapes mirror the real live
payloads captured during the Phase 2I investigation (NVDA/MA/AAL/FTEC)."""

from datetime import date, datetime

import pandas as pd
import pytest
import yfinance.exceptions as yf_exceptions

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.providers.yfinance_provider import YFinanceProvider

_NVDA_INFO = {
    "mostRecentQuarter": 1785024000,
    "nextFiscalYearEnd": 1800835200,  # 2027-01-25
}

_UPGRADES_DOWNGRADES = pd.DataFrame(
    {
        "Firm": ["Piper Sandler", "Rosenblatt", "Melius Research"],
        "ToGrade": ["Overweight", "Buy", "Hold"],
        "FromGrade": [None, "Buy", "Buy"],
        "Action": ["init", "main", "down"],
        "priceTargetAction": ["Announces", "Maintains", "Lowers"],
        "currentPriceTarget": [300.0, 390.0, 19.0],
        "priorPriceTarget": [0.0, 390.0, 24.0],
    },
    index=pd.DatetimeIndex(
        ["2026-09-10 14:02:40", "2026-09-04 11:38:09", "2026-07-07 17:35:28"],
        name="GradeDate",
    ),
)

_EPS_TREND = pd.DataFrame(
    {
        "current": [9.30456, 15.56753],
        "7daysAgo": [9.30741, 15.4581],
        "30daysAgo": [8.96264, 12.83803],
        "60daysAgo": [8.9416, 12.73643],
        "90daysAgo": [8.92355, 12.67184],
        "currency": ["USD", "USD"],
    },
    index=pd.Index(["0y", "+1y"], name="period"),
)

_EPS_REVISIONS = pd.DataFrame(
    {
        "upLast7days": [2, 41],
        "upLast30days": [39, 41],
        "downLast30days": [1, 0],
        "downLast7Days": [0, 0],
        "currency": ["USD", "USD"],
    },
    index=pd.Index(["0y", "+1y"], name="period"),
)


class _FakeTicker:
    def __init__(
        self,
        *,
        info=None,
        upgrades_downgrades=None,
        eps_trend=None,
        eps_revisions=None,
        raises=None,
    ):
        self._info = info if info is not None else {}
        self._upgrades_downgrades = (
            upgrades_downgrades if upgrades_downgrades is not None else pd.DataFrame()
        )
        self._eps_trend = eps_trend if eps_trend is not None else pd.DataFrame()
        self._eps_revisions = eps_revisions if eps_revisions is not None else pd.DataFrame()
        self._raises = raises

    def get_info(self):
        if self._raises is not None:
            raise self._raises
        return self._info

    def get_upgrades_downgrades(self):
        if self._raises is not None:
            raise self._raises
        return self._upgrades_downgrades

    def get_eps_trend(self):
        if self._raises is not None:
            raise self._raises
        return self._eps_trend

    def get_eps_revisions(self):
        if self._raises is not None:
            raise self._raises
        return self._eps_revisions


def _provider_with_ticker(monkeypatch, fake_ticker):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: fake_ticker)
    return provider


# --- get_analyst_rating_changes ---------------------------------------------


def test_get_analyst_rating_changes_parses_real_shaped_history(monkeypatch):
    fake = _FakeTicker(upgrades_downgrades=_UPGRADES_DOWNGRADES)
    provider = _provider_with_ticker(monkeypatch, fake)
    events = provider.get_analyst_rating_changes("NVDA")
    assert len(events) == 3
    assert events[0]["grade_date"] == datetime(2026, 9, 10, 14, 2, 40)
    assert events[0]["firm"] == "Piper Sandler"
    assert events[0]["action"] == "init"


def test_get_analyst_rating_changes_normalizes_zero_price_target_to_none(monkeypatch):
    """A brand-new initiation has no genuine "prior" target -- yfinance
    reports 0.0, which is normalized to None, never presented as a real
    $0 price target."""
    fake = _FakeTicker(upgrades_downgrades=_UPGRADES_DOWNGRADES)
    provider = _provider_with_ticker(monkeypatch, fake)
    events = provider.get_analyst_rating_changes("NVDA")
    initiation = next(event for event in events if event["action"] == "init")
    assert initiation["prior_price_target"] is None
    assert initiation["from_grade"] is None
    assert initiation["current_price_target"] == 300.0


def test_get_analyst_rating_changes_returns_empty_list_when_no_coverage(monkeypatch):
    """Confirmed live ETF behaviour: yfinance returns an empty frame via a
    handled 404, not an exception."""
    fake = _FakeTicker(upgrades_downgrades=pd.DataFrame())
    provider = _provider_with_ticker(monkeypatch, fake)
    assert provider.get_analyst_rating_changes("FTEC") == []


def test_get_analyst_rating_changes_propagates_classified_provider_error(monkeypatch):
    fake = _FakeTicker(raises=yf_exceptions.YFRateLimitError())
    provider = _provider_with_ticker(monkeypatch, fake)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_analyst_rating_changes("NVDA")
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED


# --- get_estimate_revision_trend --------------------------------------------


def test_get_estimate_revision_trend_parses_real_shaped_data(monkeypatch):
    fake = _FakeTicker(
        info=_NVDA_INFO, eps_trend=_EPS_TREND, eps_revisions=_EPS_REVISIONS
    )
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = provider.get_estimate_revision_trend("NVDA", date(2026, 9, 14))
    by_period = {item["fiscal_period"]: item for item in observations}
    assert set(by_period) == {date(2027, 1, 25), date(2028, 1, 25)}
    current_year = by_period[date(2027, 1, 25)]
    assert current_year["eps_trend_current"] == 9.30456
    assert current_year["eps_trend_90d_ago"] == 8.92355
    assert current_year["revisions_up_last_7d"] == 2
    assert current_year["revisions_down_last_30d"] == 1


def test_get_estimate_revision_trend_only_captures_precise_annual_periods(monkeypatch):
    """Mirrors get_estimates's deliberate quarterly-period exclusion: the
    same anchor-derivation helpers are reused, so a stray "0q"/"+1q" row in
    the trend frame must never be captured either."""
    trend_with_quarterly = pd.concat(
        [
            pd.DataFrame(
                {"current": [2.47], "7daysAgo": [2.46], "30daysAgo": [2.34],
                 "60daysAgo": [2.33], "90daysAgo": [2.33], "currency": ["USD"]},
                index=pd.Index(["0q"], name="period"),
            ),
            _EPS_TREND,
        ]
    )
    fake = _FakeTicker(info=_NVDA_INFO, eps_trend=trend_with_quarterly, eps_revisions=_EPS_REVISIONS)
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = provider.get_estimate_revision_trend("NVDA", date(2026, 9, 14))
    periods = {item["fiscal_period"] for item in observations}
    assert periods == {date(2027, 1, 25), date(2028, 1, 25)}


def test_get_estimate_revision_trend_missing_revisions_frame_leaves_counts_none(monkeypatch):
    fake = _FakeTicker(info=_NVDA_INFO, eps_trend=_EPS_TREND, eps_revisions=pd.DataFrame())
    provider = _provider_with_ticker(monkeypatch, fake)
    observations = provider.get_estimate_revision_trend("NVDA", date(2026, 9, 14))
    for item in observations:
        assert item["revisions_up_last_7d"] is None
        assert item["revisions_down_last_30d"] is None
        assert item["eps_trend_current"] is not None


def test_get_estimate_revision_trend_returns_empty_list_when_no_coverage(monkeypatch):
    fake = _FakeTicker(info={}, eps_trend=pd.DataFrame(), eps_revisions=pd.DataFrame())
    provider = _provider_with_ticker(monkeypatch, fake)
    assert provider.get_estimate_revision_trend("FTEC", date(2026, 9, 14)) == []


def test_get_estimate_revision_trend_propagates_classified_provider_error(monkeypatch):
    fake = _FakeTicker(raises=yf_exceptions.YFRateLimitError())
    provider = _provider_with_ticker(monkeypatch, fake)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_estimate_revision_trend("NVDA", date(2026, 9, 14))
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
