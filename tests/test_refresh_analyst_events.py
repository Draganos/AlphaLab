"""Deterministic tests for scripts/refresh_analyst_events.py's per-ticker
failure isolation, no-coverage classification, and idempotency. No network
access."""

from datetime import datetime
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import AnalystRatingChange
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_analyst_events.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("refresh_analyst_events_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script_module()


class _FakeProvider:
    provider_name = "FakeAnalystEventProvider"

    def __init__(self, *, events=None, failing=None, empty=()):
        self._events = events or {}
        self._failing = failing or {}
        self._empty = set(empty)

    def get_analyst_rating_changes(self, ticker):
        if ticker in self._failing:
            raise self._failing[ticker]
        if ticker in self._empty:
            return []
        return self._events.get(ticker, [])


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_stores_real_events_and_is_idempotent_on_rerun(script, engine):
    provider = _FakeProvider(
        events={"NVDA": [{"grade_date": datetime(2026, 9, 10), "firm": "Piper Sandler",
                           "to_grade": "Overweight", "from_grade": None, "action": "init",
                           "price_target_action": "Announces", "current_price_target": 300.0,
                           "prior_price_target": None}]}
    )
    first = script.refresh_analyst_events(engine, provider, ["NVDA"])
    assert first.stored == {"NVDA": 1}
    assert first.all_succeeded

    second = script.refresh_analyst_events(engine, provider, ["NVDA"])
    assert second.stored == {"NVDA": 0}

    with Session(engine) as session:
        assert len(session.scalars(select(AnalystRatingChange)).all()) == 1


def test_no_coverage_ticker_is_not_a_failure(script, engine):
    provider = _FakeProvider(empty=("FTEC",))
    result = script.refresh_analyst_events(engine, provider, ["FTEC"])
    assert result.empty == ["FTEC"]
    assert result.failed == {}
    assert result.all_succeeded


def test_one_ticker_failure_never_aborts_the_rest_of_the_batch(script, engine):
    provider = _FakeProvider(
        events={"MA": [{"grade_date": datetime(2026, 8, 31), "firm": "RBC Capital",
                         "to_grade": "Outperform", "from_grade": "Outperform", "action": "main",
                         "price_target_action": "Raises", "current_price_target": 696.0,
                         "prior_price_target": 642.0}]},
        failing={"NVDA": ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "Fake", "down")},
    )
    result = script.refresh_analyst_events(engine, provider, ["NVDA", "MA"])
    assert "NVDA" in result.failed
    assert result.stored == {"MA": 1}
    assert result.all_succeeded is False


def test_failed_refresh_preserves_previously_stored_events(script, engine):
    provider = _FakeProvider(
        events={"NVDA": [{"grade_date": datetime(2026, 9, 10), "firm": "Piper Sandler",
                           "to_grade": "Overweight", "from_grade": None, "action": "init",
                           "price_target_action": "Announces", "current_price_target": 300.0,
                           "prior_price_target": None}]}
    )
    script.refresh_analyst_events(engine, provider, ["NVDA"])

    failing_provider = _FakeProvider(
        failing={"NVDA": ProviderError(ProviderErrorKind.RATE_LIMITED, "Fake", "429")}
    )
    result = script.refresh_analyst_events(engine, failing_provider, ["NVDA"])
    assert "NVDA" in result.failed

    with Session(engine) as session:
        rows = session.scalars(select(AnalystRatingChange).where(AnalystRatingChange.ticker == "NVDA")).all()
    assert len(rows) == 1
    assert rows[0].firm == "Piper Sandler"
