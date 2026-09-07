"""Offline, deterministic tests for MacroRegimeService: ingestion-then-compute
orchestration, hash-dedup, and per-ticker failure resilience. No network
access -- uses a fake in-memory MarketDataProvider."""

from datetime import date

import pandas as pd
import pytest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.macro.service import DEFAULT_MACRO_SCOPE, MacroRegimeService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

_VALUES = {
    "^VIX": 12.0, "^TNX": 45.0, "^IRX": 40.0,
    "DX-Y.NYB": 100.0, "CL=F": 70.0, "GC=F": 2000.0,
}


class _FakeMacroProvider(MarketDataProvider):
    provider_name = "FakeMacroProvider"

    def __init__(self, fail_tickers: frozenset[str] = frozenset(), overrides: dict | None = None):
        self._fail = fail_tickers
        self._overrides = overrides or {}

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"Fixture {ticker}", "asset_type": "INDEX", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        if ticker in self._fail:
            raise ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, self.provider_name, "simulated failure")
        value = self._overrides.get(ticker, _VALUES.get(ticker, 50.0))
        days = 60
        idx = pd.date_range(end=pd.Timestamp(end), periods=days)
        return pd.DataFrame({"close": [value] * days, "high": [value] * days, "low": [value] * days}, index=idx)

    def get_financials(self, ticker):
        return pd.DataFrame()


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_refresh_ingests_and_computes_a_full_coverage_risk_on_regime(engine):
    service = MacroRegimeService(engine)
    current = service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    assert current.scope == DEFAULT_MACRO_SCOPE
    assert current.regime == MacroRegime.RISK_ON.value
    assert current.coverage == 1.0

    read_back = service.get_current()
    assert read_back is not None
    assert read_back.content_hash == current.content_hash


def test_first_refresh_creates_current_state_and_one_historical_snapshot(engine):
    service = MacroRegimeService(engine)
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    history = service.get_history()
    assert len(history) == 1
    assert history[0].regime == MacroRegime.RISK_ON.value


def test_identical_substantive_content_creates_no_duplicate_snapshot_across_days(engine):
    """A day passing (and therefore every proxy's latest price date
    advancing) must not by itself create a new historical snapshot when the
    substantive regime read hasn't changed."""
    service = MacroRegimeService(engine)
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 2))
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 3))
    assert len(service.get_history()) == 1


def test_changed_regime_creates_a_new_historical_snapshot(engine):
    service = MacroRegimeService(engine)
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))  # calm -> RISK_ON
    service.refresh(
        _FakeMacroProvider(overrides={"^VIX": 35.0, "^TNX": 40.0, "^IRX": 50.0}),
        as_of=date(2024, 6, 2),
    )  # fearful + inverted -> RISK_OFF
    history = service.get_history()
    assert len(history) == 2
    regimes = {row.regime for row in history}
    assert regimes == {MacroRegime.RISK_ON.value, MacroRegime.RISK_OFF.value}


def test_one_ticker_failing_ingestion_does_not_abort_the_others(engine):
    service = MacroRegimeService(engine)
    current = service.refresh(_FakeMacroProvider(fail_tickers=frozenset({"^VIX"})), as_of=date(2024, 6, 1))
    assert current.coverage < 1.0
    assessment = service.get_current_assessment()
    vix = next(i for i in assessment.indicators if i.ticker == "^VIX")
    assert vix.value is None
    # Yield curve alone is still available -> still assessable, not REVIEW.
    assert current.regime != MacroRegime.REVIEW.value


def test_a_later_ingestion_failure_does_not_erase_previously_ingested_price_history(engine):
    """The core failure-preservation invariant, applied to macro regime:
    once a proxy ticker's price history exists, a later failed ingestion
    for that same ticker leaves the stored history (and therefore its
    contribution to coverage) untouched."""
    service = MacroRegimeService(engine)
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    full_coverage = service.get_current().coverage

    degraded = service.refresh(_FakeMacroProvider(fail_tickers=frozenset({"^VIX"})), as_of=date(2024, 6, 2))
    assert degraded.coverage == full_coverage  # stale-but-real VIX data still counted


def test_get_current_and_get_history_are_empty_before_any_refresh(engine):
    service = MacroRegimeService(engine)
    assert service.get_current() is None
    assert service.get_current_assessment() is None
    assert service.get_history() == []


def test_refresh_never_uses_price_rows_dated_after_as_of(engine):
    """Point-in-time regression: a database that already holds price rows
    dated after `as_of` (e.g. from a later, unrelated refresh) must never
    leak those future observations into a historical assessment.

    Fails against the pre-fix implementation, which read a ticker's full
    price history with no `Price.date <= as_of` filter at all."""
    from sqlalchemy.orm import Session

    from alpha_lab.database.models import Price
    from alpha_lab.macro.regime import MacroRegime

    service = MacroRegimeService(engine)
    service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))  # calm VIX=12 -> RISK_ON

    # Simulate a future, extreme VIX spike already sitting in the database
    # (e.g. ingested by a later refresh) dated well after as_of.
    with Session(engine) as session:
        session.add(Price(
            ticker="^VIX", date=date(2024, 12, 1), close=90.0, high=90.0, low=90.0,
            provider="test", source="future-injection",
        ))
        session.commit()

    # Recomputing the SAME historical as_of must be completely unaffected
    # by that future row.
    result = service.refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    assessment = service.get_current_assessment()
    vix = next(i for i in assessment.indicators if i.ticker == "^VIX")
    assert vix.value == pytest.approx(12.0)
    assert result.regime == MacroRegime.RISK_ON.value
