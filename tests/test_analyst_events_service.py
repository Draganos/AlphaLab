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


# --- PR #32 (roadmap): as_of must be genuinely point-in-time-safe -----------
#
# yfinance's upgradeDowngradeHistory/earningsTrend backfill a ticker's
# *entire* history in one refresh call (see AnalystRatingChange/
# EstimateRevisionTrend's own docstrings) -- a row whose claimed grade_date/
# observation_date is years in the past can still have been inserted into
# these tables only today. Filtering an "as of the past" read on those
# claimed dates alone would silently leak that not-yet-ingested history.
# These tests set retrieved_at/ingested_at directly (bypassing the
# snapshot_* helpers, which always stamp "now") to simulate a row AlphaLab
# genuinely had not yet ingested as of the requested as_of.

def _set_retrieved_at(engine, ticker: str, grade_date: datetime, retrieved_at: datetime) -> None:
    with Session(engine) as session:
        row = session.scalar(
            select(AnalystRatingChange).where(
                AnalystRatingChange.ticker == ticker, AnalystRatingChange.grade_date == grade_date
            )
        )
        row.retrieved_at = retrieved_at
        session.commit()


def _set_ingested_at(engine, ticker: str, fiscal_period: date, ingested_at: datetime) -> None:
    with Session(engine) as session:
        row = session.scalar(
            select(EstimateRevisionTrend).where(
                EstimateRevisionTrend.ticker == ticker,
                EstimateRevisionTrend.fiscal_period == fiscal_period,
            )
        )
        row.ingested_at = ingested_at
        session.commit()


def test_get_rating_changes_as_of_excludes_an_event_not_yet_ingested_by_that_date(engine):
    """The event's grade_date (2026, 1, 1) predates as_of, but it was only
    actually ingested (retrieved_at) after as_of -- a historical read as of
    that date must not see it."""
    old_grade_date = datetime(2026, 1, 1)
    snapshot_analyst_rating_changes(
        engine, "NVDA", [{**_EVENT, "grade_date": old_grade_date}], provider="FakeProvider"
    )
    _set_retrieved_at(engine, "NVDA", old_grade_date, datetime(2026, 9, 1))
    service = AnalystEventsService(engine)

    as_of_before_ingestion = service.get_rating_changes("NVDA", as_of=date(2026, 6, 1))
    as_of_after_ingestion = service.get_rating_changes("NVDA", as_of=date(2026, 9, 2))
    unfiltered_current_view = service.get_rating_changes("NVDA")

    assert as_of_before_ingestion == []
    assert len(as_of_after_ingestion) == 1
    assert len(unfiltered_current_view) == 1


def test_get_latest_revision_trend_as_of_excludes_an_observation_not_yet_ingested(engine):
    fiscal_period = _TREND_OBSERVATION["fiscal_period"]
    snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 1, 1), [_TREND_OBSERVATION], provider="FakeProvider"
    )
    _set_ingested_at(engine, "NVDA", fiscal_period, datetime(2026, 9, 1))
    service = AnalystEventsService(engine)

    as_of_before_ingestion = service.get_latest_revision_trend("NVDA", as_of=date(2026, 6, 1))
    as_of_after_ingestion = service.get_latest_revision_trend("NVDA", as_of=date(2026, 9, 2))

    assert as_of_before_ingestion == []
    assert len(as_of_after_ingestion) == 1


def test_get_latest_revision_trend_per_period_selection_uses_ingested_at_not_observation_date(engine):
    """Self-review finding: the per-fiscal_period "latest" pick must be
    decided by ingested_at, not observation_date -- observation_date is
    caller-controlled (see refresh_revision_trend's own as_of argument),
    so two rows for the same period can have an observation_date order
    that disagrees with the order AlphaLab actually ingested them in."""
    fiscal_period = _TREND_OBSERVATION["fiscal_period"]
    earlier_observation_date_row = {**_TREND_OBSERVATION, "eps_trend_current": 1.0}
    later_observation_date_row = {**_TREND_OBSERVATION, "eps_trend_current": 2.0}
    snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 1, 1), [earlier_observation_date_row], provider="FakeProvider"
    )
    snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 9, 14), [later_observation_date_row], provider="FakeProvider"
    )
    # Force the real ingestion order to be the OPPOSITE of the
    # observation_date order.
    with Session(engine) as session:
        rows = session.scalars(
            select(EstimateRevisionTrend).where(
                EstimateRevisionTrend.ticker == "NVDA",
                EstimateRevisionTrend.fiscal_period == fiscal_period,
            )
        ).all()
        early_by_date = next(r for r in rows if r.observation_date == date(2026, 1, 1))
        late_by_date = next(r for r in rows if r.observation_date == date(2026, 9, 14))
        early_by_date.ingested_at = datetime(2026, 9, 20)  # ingested most recently
        late_by_date.ingested_at = datetime(2026, 1, 2)  # ingested first, despite the later observation_date
        session.commit()

    service = AnalystEventsService(engine)
    rows = service.get_latest_revision_trend("NVDA")

    assert len(rows) == 1
    assert rows[0].eps_trend_current == 1.0


def test_get_research_summary_as_of_never_leaks_evidence_not_yet_ingested(engine):
    """End-to-end: a summary requested as of a past date must reflect only
    what AlphaLab had actually ingested by then, for both evidence layers."""
    old_grade_date = datetime(2026, 1, 1)
    snapshot_analyst_rating_changes(
        engine, "NVDA", [{**_EVENT, "grade_date": old_grade_date}], provider="FakeProvider"
    )
    _set_retrieved_at(engine, "NVDA", old_grade_date, datetime(2026, 9, 1))
    snapshot_estimate_revisions(
        engine, "NVDA", date(2026, 1, 1), [_TREND_OBSERVATION], provider="FakeProvider"
    )
    _set_ingested_at(engine, "NVDA", _TREND_OBSERVATION["fiscal_period"], datetime(2026, 9, 1))
    service = AnalystEventsService(engine)

    historical = service.get_research_summary("NVDA", as_of=date(2026, 6, 1))
    current = service.get_research_summary("NVDA", as_of=date(2026, 9, 2))

    assert historical is None  # nothing had actually been ingested by then
    assert current is not None
    assert len(current.recent_rating_changes) == 1
    assert len(current.revision_trend) == 1


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
    summary = service.get_research_summary("NVDA")  # as_of omitted: current view, no PIT filter
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
