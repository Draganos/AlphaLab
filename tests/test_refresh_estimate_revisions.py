"""Deterministic tests for scripts/refresh_estimate_revisions.py's
per-ticker failure isolation, no-coverage classification, and idempotency.
No network access."""

from datetime import date
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import EstimateRevisionTrend
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_estimate_revisions.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("refresh_estimate_revisions_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script_module()


class _FakeProvider:
    provider_name = "FakeEstimateRevisionProvider"

    def __init__(self, *, observations=None, failing=None, empty=()):
        self._observations = observations or {}
        self._failing = failing or {}
        self._empty = set(empty)

    def get_estimate_revision_trend(self, ticker, observation_date):
        if ticker in self._failing:
            raise self._failing[ticker]
        if ticker in self._empty:
            return []
        return self._observations.get(ticker, [])


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


_OBSERVATION = {
    "fiscal_period": date(2027, 1, 25),
    "eps_trend_current": 9.30456,
    "eps_trend_7d_ago": 9.30741,
    "eps_trend_30d_ago": 8.96264,
    "eps_trend_60d_ago": 8.9416,
    "eps_trend_90d_ago": 8.92355,
    "revisions_up_last_7d": 2,
    "revisions_up_last_30d": 39,
    "revisions_down_last_7d": 0,
    "revisions_down_last_30d": 1,
    "currency": "USD",
}


def test_stores_real_observations_and_is_idempotent_on_rerun(script, engine):
    provider = _FakeProvider(observations={"NVDA": [_OBSERVATION]})
    first = script.refresh_estimate_revisions(engine, provider, ["NVDA"], date(2026, 9, 14))
    assert first.stored == {"NVDA": 1}
    assert first.all_succeeded

    second = script.refresh_estimate_revisions(engine, provider, ["NVDA"], date(2026, 9, 14))
    assert second.stored == {"NVDA": 0}

    with Session(engine) as session:
        assert len(session.scalars(select(EstimateRevisionTrend)).all()) == 1


def test_no_coverage_ticker_is_not_a_failure(script, engine):
    provider = _FakeProvider(empty=("FTEC",))
    result = script.refresh_estimate_revisions(engine, provider, ["FTEC"], date(2026, 9, 14))
    assert result.empty == ["FTEC"]
    assert result.failed == {}
    assert result.all_succeeded


def test_one_ticker_failure_never_aborts_the_rest_of_the_batch(script, engine):
    provider = _FakeProvider(
        observations={"MA": [{**_OBSERVATION, "fiscal_period": date(2026, 12, 31)}]},
        failing={"NVDA": ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "Fake", "down")},
    )
    result = script.refresh_estimate_revisions(engine, provider, ["NVDA", "MA"], date(2026, 9, 14))
    assert "NVDA" in result.failed
    assert result.stored == {"MA": 1}
    assert result.all_succeeded is False


def test_failed_refresh_preserves_previously_stored_observations(script, engine):
    provider = _FakeProvider(observations={"NVDA": [_OBSERVATION]})
    script.refresh_estimate_revisions(engine, provider, ["NVDA"], date(2026, 9, 14))

    failing_provider = _FakeProvider(
        failing={"NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "Fake", "429")}
    )
    result = script.refresh_estimate_revisions(engine, failing_provider, ["NVDA"], date(2026, 9, 15))
    assert "NVDA" in result.failed

    with Session(engine) as session:
        rows = session.scalars(select(EstimateRevisionTrend).where(EstimateRevisionTrend.ticker == "NVDA")).all()
    assert len(rows) == 1
    assert rows[0].eps_trend_current == 9.30456
