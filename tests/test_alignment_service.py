"""Offline, deterministic tests for AlignmentService: persistence,
hash-dedup, degradation, and point-in-time safety. No network access --
uses fake in-memory providers for the two underlying evidence layers."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from alpha_lab.alignment.alignment import Alignment
from alpha_lab.alignment.service import AlignmentService
from alpha_lab.calibration.service import DEFAULT_CALIBRATION_SOURCE, ExternalCalibrationService
from alpha_lab.database import create_schema, make_engine
from alpha_lab.macro.service import DEFAULT_MACRO_SCOPE, MacroRegimeService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.donatien import DonatienCalibration, DonatienFetchResult

_CALM_VALUES = {"^VIX": 12.0, "^TNX": 45.0, "^IRX": 40.0, "DX-Y.NYB": 100.0, "CL=F": 70.0, "GC=F": 2000.0}
_FEARFUL_VALUES = {"^VIX": 35.0, "^TNX": 40.0, "^IRX": 50.0, "DX-Y.NYB": 100.0, "CL=F": 70.0, "GC=F": 2000.0}

_CONSTRUCTIVE_WEIGHTS = {"Soft Landing": 60, "Reacceleration": 20, "Stagflation": 10, "Deflationary Bust": 10}
_DEFENSIVE_WEIGHTS = {"Stagflation": 40, "Deflationary Bust": 35, "Soft Landing": 15, "Reacceleration": 10}


class _FakeMacroProvider(MarketDataProvider):
    provider_name = "FakeMacroProvider"

    def __init__(self, values: dict[str, float]):
        self._values = values

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"Fixture {ticker}", "asset_type": "INDEX", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        value = self._values.get(ticker, 50.0)
        idx = pd.date_range(end=pd.Timestamp(end), periods=60)
        return pd.DataFrame({"close": [value] * 60, "high": [value] * 60, "low": [value] * 60}, index=idx)

    def get_financials(self, ticker):
        return pd.DataFrame()


class _FakeDonatienProvider:
    def __init__(self, scenario_weights: dict[str, float], *, run_date: date, retrieved_at: datetime):
        self._scenario_weights = scenario_weights
        self._run_date = run_date
        self._retrieved_at = retrieved_at

    def fetch(self) -> DonatienFetchResult:
        calibration = DonatienCalibration(
            run_date=self._run_date, run_time="20:15", macro_report_date=self._run_date,
            dominant_regime="Fixture regime label", confidence="Medium",
            scenario_weights=self._scenario_weights, defensiveness=4.0,
            top_drivers=[], key_changes=[], trend_contrarian_split={}, tiers={},
        )
        raw_payload = calibration.model_dump(mode="json")
        return DonatienFetchResult(
            calibration=calibration, raw_payload=raw_payload,
            content_hash=f"fixture-hash-{self._run_date.isoformat()}-{self._retrieved_at.isoformat()}",
            retrieved_at=self._retrieved_at, source_url="https://donatien.example/fixture",
        )


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(engine, *, macro_values, donatien_weights, as_of: date, retrieved_at: datetime):
    MacroRegimeService(engine).refresh(_FakeMacroProvider(macro_values), as_of=as_of)
    ExternalCalibrationService(engine).refresh(
        _FakeDonatienProvider(donatien_weights, run_date=as_of, retrieved_at=retrieved_at)
    )


# --- basic refresh / persistence --------------------------------------------


def test_refresh_computes_and_persists_aligned_current_state(engine):
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    current = AlignmentService(engine).refresh(as_of=date(2024, 6, 1))
    assert current.scope == DEFAULT_MACRO_SCOPE
    assert current.alignment == Alignment.ALIGNED.value

    read_back = AlignmentService(engine).get_current()
    assert read_back is not None
    assert read_back.content_hash == current.content_hash


def test_first_refresh_creates_one_historical_snapshot(engine):
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    AlignmentService(engine).refresh(as_of=date(2024, 6, 1))
    history = AlignmentService(engine).get_history()
    assert len(history) == 1
    assert history[0].alignment == Alignment.ALIGNED.value


def test_get_current_and_get_history_are_empty_before_any_refresh(engine):
    service = AlignmentService(engine)
    assert service.get_current() is None
    assert service.get_current_assessment() is None
    assert service.get_history() == []


# --- degradation: missing evidence on either side ---------------------------


def test_missing_donatien_evidence_produces_insufficient_data_not_a_fabricated_value(engine):
    MacroRegimeService(engine).refresh(_FakeMacroProvider(_CALM_VALUES), as_of=date(2024, 6, 1))
    # Donatien never refreshed at all.
    current = AlignmentService(engine).refresh(as_of=date(2024, 6, 1))
    assert current.alignment == Alignment.INSUFFICIENT_DATA.value


def test_missing_macro_evidence_produces_insufficient_data_not_a_fabricated_value(engine):
    ExternalCalibrationService(engine).refresh(
        _FakeDonatienProvider(_CONSTRUCTIVE_WEIGHTS, run_date=date(2024, 6, 1),
                               retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    )
    # Macro regime never refreshed at all.
    current = AlignmentService(engine).refresh(as_of=date(2024, 6, 1))
    assert current.alignment == Alignment.INSUFFICIENT_DATA.value


# --- content-hash dedup ------------------------------------------------------


def test_identical_substantive_content_creates_no_duplicate_snapshot_across_days(engine):
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    service = AlignmentService(engine)
    service.refresh(as_of=date(2024, 6, 1))
    service.refresh(as_of=date(2024, 6, 2))
    service.refresh(as_of=date(2024, 6, 3))
    assert len(service.get_history()) == 1


def test_changed_alignment_creates_a_new_historical_snapshot(engine):
    service = AlignmentService(engine)
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    service.refresh(as_of=date(2024, 6, 1))  # RISK_ON + CONSTRUCTIVE -> ALIGNED

    _seed(engine, macro_values=_FEARFUL_VALUES, donatien_weights=_DEFENSIVE_WEIGHTS,
          as_of=date(2024, 6, 2), retrieved_at=datetime(2024, 6, 2, 10, 0, tzinfo=UTC))
    service.refresh(as_of=date(2024, 6, 2))  # RISK_OFF + DEFENSIVE -> ALIGNED (same label, different regimes)

    history = service.get_history()
    assert len(history) == 2  # same `alignment` label, but substantively different market_regime/donatien_lean


# --- point-in-time safety ----------------------------------------------------


def test_a_later_donatien_snapshot_does_not_affect_an_earlier_alignment(engine):
    """A Donatien re-run retrieved after `as_of` must never change the
    alignment computed for that earlier `as_of` -- the historical lookup
    filters on `retrieved_at <= as_of`, not on the source's own run_date."""
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    service = AlignmentService(engine)
    baseline = service.refresh(as_of=date(2024, 6, 1))
    assert baseline.alignment == Alignment.ALIGNED.value

    # A later, opposing Donatien observation retrieved well after 2024-06-01.
    ExternalCalibrationService(engine).refresh(
        _FakeDonatienProvider(_DEFENSIVE_WEIGHTS, run_date=date(2024, 12, 1),
                               retrieved_at=datetime(2024, 12, 1, 10, 0, tzinfo=UTC))
    )

    recomputed = service.refresh(as_of=date(2024, 6, 1))
    assert recomputed.alignment == Alignment.ALIGNED.value
    assert recomputed.content_hash == baseline.content_hash


def test_a_later_macro_snapshot_does_not_affect_an_earlier_alignment(engine):
    """A future, opposing macro regime observation (dated after `as_of`)
    already sitting in the database must never leak into a historical
    alignment recomputation for an earlier `as_of`."""
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    service = AlignmentService(engine)
    baseline = service.refresh(as_of=date(2024, 6, 1))
    assert baseline.alignment == Alignment.ALIGNED.value

    # A later, opposing macro regime observation.
    MacroRegimeService(engine).refresh(_FakeMacroProvider(_FEARFUL_VALUES), as_of=date(2024, 12, 1))

    recomputed = service.refresh(as_of=date(2024, 6, 1))
    assert recomputed.alignment == Alignment.ALIGNED.value
    assert recomputed.content_hash == baseline.content_hash


def test_historical_as_of_uses_the_nearest_prior_snapshot_not_the_latest_overall(engine):
    """Requesting alignment for a date between two real observations must
    use the earlier one, never silently substitute the more recent one."""
    _seed(engine, macro_values=_CALM_VALUES, donatien_weights=_CONSTRUCTIVE_WEIGHTS,
          as_of=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    _seed(engine, macro_values=_FEARFUL_VALUES, donatien_weights=_DEFENSIVE_WEIGHTS,
          as_of=date(2024, 6, 10), retrieved_at=datetime(2024, 6, 10, 10, 0, tzinfo=UTC))

    service = AlignmentService(engine)
    mid_period = service.refresh(as_of=date(2024, 6, 5))
    assert mid_period.alignment == Alignment.ALIGNED.value  # still the 6/1 calm+constructive pairing
    later = service.refresh(as_of=date(2024, 6, 10))
    assert later.alignment == Alignment.ALIGNED.value  # now the 6/10 fearful+defensive pairing
    assert mid_period.content_hash != later.content_hash
