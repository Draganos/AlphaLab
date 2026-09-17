"""Deterministic tests for alpha_lab.ingestion.analyst_events,
alpha_lab.ingestion.estimate_revisions, and alpha_lab.research.analyst_events.
No network access."""

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import AnalystRatingChange, EstimateRevisionTrend
from alpha_lab.ingestion.analyst_events import snapshot_analyst_rating_changes
from alpha_lab.ingestion.estimate_revisions import snapshot_estimate_revisions
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.research.analyst_events import AnalystEventsService


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


_EVENT = {
    "grade_date": datetime(2026, 9, 10, 14, 2, 40),
    "firm": "Piper Sandler",
    "to_grade": "Overweight",
    "from_grade": None,
    "action": "init",
    "price_target_action": "Announces",
    "current_price_target": 300.0,
    "prior_price_target": None,
}

_TREND_OBSERVATION = {
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


# --- ingestion: content-hash idempotency ------------------------------------


def test_snapshot_analyst_rating_changes_is_idempotent_on_rerun(engine):
    first = snapshot_analyst_rating_changes(
        engine, "NVDA", [_EVENT], provider="FakeProvider"
    )
    assert first == 1
    second = snapshot_analyst_rating_changes(
        engine, "NVDA", [_EVENT], provider="FakeProvider"
    )
    assert second == 0
    with Session(engine) as session:
        assert len(session.scalars(select(AnalystRatingChange)).all()) == 1


def test_snapshot_estimate_revisions_is_idempotent_on_rerun(engine):
    first = snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 9, 14), [_TREND_OBSERVATION], provider="FakeProvider"
    )
    assert first == 1
    second = snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 9, 14), [_TREND_OBSERVATION], provider="FakeProvider"
    )
    assert second == 0
    with Session(engine) as session:
        assert len(session.scalars(select(EstimateRevisionTrend)).all()) == 1


def test_snapshot_estimate_revisions_stores_a_new_row_for_a_genuinely_different_observation(engine):
    """A later observation_date with a changed eps_trend_current is a
    distinct, genuine new snapshot -- not deduped away."""
    snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 9, 14), [_TREND_OBSERVATION], provider="FakeProvider"
    )
    changed = {**_TREND_OBSERVATION, "eps_trend_current": 9.5}
    inserted = snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 9, 21), [changed], provider="FakeProvider"
    )
    assert inserted == 1
    with Session(engine) as session:
        assert len(session.scalars(select(EstimateRevisionTrend)).all()) == 2


# --- AnalystEventsService reads ---------------------------------------------


def test_get_rating_changes_orders_most_recent_first_and_respects_limit(engine):
    older = {**_EVENT, "grade_date": datetime(2026, 1, 1)}
    newer = {**_EVENT, "grade_date": datetime(2026, 9, 1)}
    snapshot_analyst_rating_changes(engine, "NVDA", [older, newer], provider="FakeProvider")
    service = AnalystEventsService(engine)
    rows = service.get_rating_changes("NVDA", limit=1)
    assert len(rows) == 1
    assert rows[0].grade_date == datetime(2026, 9, 1)


def test_get_latest_revision_trend_returns_only_the_newest_observation_per_period(engine):
    older = {**_TREND_OBSERVATION, "eps_trend_current": 8.0}
    newer = {**_TREND_OBSERVATION, "eps_trend_current": 9.30456}
    snapshot_estimate_revisions(engine, "NVDA", date(2026, 9, 1), [older], provider="FakeProvider")
    snapshot_estimate_revisions(engine, "NVDA", date(2026, 9, 14), [newer], provider="FakeProvider")
    service = AnalystEventsService(engine)
    rows = service.get_latest_revision_trend("NVDA")
    assert len(rows) == 1
    assert rows[0].eps_trend_current == 9.30456
    assert rows[0].observation_date == date(2026, 9, 14)


def test_get_research_summary_shows_up_to_20_recent_changes_by_default(engine):
    """Regression: get_research_summary must match the Company Research
    UI's pre-existing display count (get_rating_changes(ticker, limit=20)),
    not silently fall back to build_analyst_research_summary's own smaller
    default (10), which would drop real events from view with no
    indication anything was hidden."""
    events = [
        {**_EVENT, "grade_date": datetime(2026, 1, day)} for day in range(1, 16)
    ]  # 15 distinct events -- more than the smaller default, fewer than 20
    snapshot_analyst_rating_changes(engine, "NVDA", events, provider="FakeProvider")
    service = AnalystEventsService(engine)
    summary = service.get_research_summary("NVDA", as_of=date(2026, 9, 14))
    assert len(summary.recent_rating_changes) == 15


# --- AnalystEventsService.refresh_all: independent per-domain failure ------


class _FakeAnalystEventProvider:
    provider_name = "FakeAnalystEventProvider"

    def __init__(self, *, events=None, trend=None, events_error=None, trend_error=None):
        self._events = events or []
        self._trend = trend or []
        self._events_error = events_error
        self._trend_error = trend_error

    def get_analyst_rating_changes(self, ticker):
        if self._events_error is not None:
            raise self._events_error
        return self._events

    def get_estimate_revision_trend(self, ticker, observation_date):
        if self._trend_error is not None:
            raise self._trend_error
        return self._trend


def test_refresh_all_succeeds_independently_when_one_domain_has_no_coverage(engine):
    provider = _FakeAnalystEventProvider(events=[_EVENT], trend=[])
    service = AnalystEventsService(engine)
    outcome = service.refresh_all("NVDA", provider)
    assert outcome.rating_changes_stored == 1
    assert outcome.revision_trend_stored == 0
    assert outcome.rating_changes_error is None
    assert outcome.revision_trend_error is None


def test_refresh_all_one_domain_failure_never_blocks_the_other(engine):
    provider = _FakeAnalystEventProvider(
        events=[_EVENT],
        trend_error=ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "Fake", "down"),
    )
    service = AnalystEventsService(engine)
    outcome = service.refresh_all("NVDA", provider)
    assert outcome.rating_changes_stored == 1
    assert outcome.rating_changes_error is None
    assert outcome.revision_trend_error is not None
    assert outcome.revision_trend_error.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_refresh_all_failure_preserves_previously_stored_rating_changes(engine):
    """A failed later refresh must never erase events a prior successful
    refresh already persisted -- both tables are append-only, so there is
    nothing to overwrite, but this proves it end to end via the service."""
    good_provider = _FakeAnalystEventProvider(events=[_EVENT])
    service = AnalystEventsService(engine)
    service.refresh_all("NVDA", good_provider)

    failing_provider = _FakeAnalystEventProvider(
        events_error=ProviderError(ProviderErrorKind.RATE_LIMITED, "Fake", "429")
    )
    service.refresh_all("NVDA", failing_provider)

    rows = service.get_rating_changes("NVDA")
    assert len(rows) == 1
    assert rows[0].firm == "Piper Sandler"
