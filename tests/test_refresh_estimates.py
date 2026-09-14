"""Deterministic tests for scripts/refresh_estimates.py's per-ticker failure
isolation, no-coverage classification, idempotency, and failure-preservation
semantics. No network access."""

from datetime import date
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Estimate
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_estimates.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("refresh_estimates_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script_module()


class _FakeProvider:
    provider_name = "FakeEstimateProvider"

    def __init__(self, *, observations=None, failing=None, empty=()):
        self._observations = observations or {}
        self._failing = failing or {}
        self._empty = set(empty)

    def get_estimates(self, ticker, observation_date):
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


def test_stores_real_observations_and_is_idempotent_on_rerun(script, engine):
    provider = _FakeProvider(
        observations={"NVDA": [{"fiscal_period": date(2027, 1, 25), "consensus_eps": 9.3}]}
    )
    first = script.refresh_estimates(engine, provider, ["NVDA"], date(2026, 9, 14))
    assert first.stored == {"NVDA": 1}
    assert first.all_succeeded

    second = script.refresh_estimates(engine, provider, ["NVDA"], date(2026, 9, 14))
    assert second.stored == {"NVDA": 0}  # identical content -> no new row

    with Session(engine) as session:
        assert len(session.scalars(select(Estimate)).all()) == 1


def test_no_coverage_ticker_is_not_a_failure(script, engine):
    provider = _FakeProvider(empty=("FTEC",))
    result = script.refresh_estimates(engine, provider, ["FTEC"], date(2026, 9, 14))
    assert result.empty == ["FTEC"]
    assert result.failed == {}
    assert result.all_succeeded


def test_one_ticker_failure_never_aborts_the_rest_of_the_batch(script, engine):
    provider = _FakeProvider(
        observations={"MA": [{"fiscal_period": date(2026, 12, 31), "consensus_eps": 20.0}]},
        failing={"NVDA": ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "Fake", "down")},
    )
    result = script.refresh_estimates(engine, provider, ["NVDA", "MA"], date(2026, 9, 14))
    assert "NVDA" in result.failed
    assert result.stored == {"MA": 1}
    assert result.all_succeeded is False


def test_failed_refresh_preserves_previously_stored_estimates(script, engine):
    provider = _FakeProvider(
        observations={"NVDA": [{"fiscal_period": date(2027, 1, 25), "consensus_eps": 9.3}]}
    )
    script.refresh_estimates(engine, provider, ["NVDA"], date(2026, 9, 14))

    failing_provider = _FakeProvider(
        failing={"NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "Fake", "429")}
    )
    result = script.refresh_estimates(engine, failing_provider, ["NVDA"], date(2026, 9, 15))
    assert "NVDA" in result.failed

    with Session(engine) as session:
        rows = session.scalars(select(Estimate).where(Estimate.ticker == "NVDA")).all()
    assert len(rows) == 1
    assert rows[0].consensus_eps == 9.3
