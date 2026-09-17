"""Persistence/orchestration for Analyst Rating Changes and Estimate
Revision Trend evidence (Phase 2I).

Three deliberately distinct analyst-evidence layers now exist in AlphaLab,
none of which feeds any of the others or the fundamental score:

1. **Analyst Consensus** (`alpha_lab.research.analyst_consensus`) -- what
   analysts currently think, as one upserted row per ticker
   (`CurrentAnalystConsensus`): buy/hold/sell counts and price targets,
   from `recommendationTrend`'s "0m" row.
2. **Analyst Rating Changes** (this module, `AnalystRatingChange`) -- the
   discrete graded events behind that consensus: every upgrade, downgrade,
   initiation, and reiteration, each with its own real historical
   `grade_date`, append-only like `NewsArticleRecord` (no `Current*`
   counterpart -- there is no single "current rating change").
3. **Estimate Revision Trend** (this module, `EstimateRevisionTrend`) --
   the source's own already-computed EPS-consensus trend
   (current/7/30/60/90-days-ago) and analyst up/down revision counts,
   distinct from the pre-existing `alpha_lab.ratings.estimates.
   calculate_revision_factors`, which derives a revision signal only after
   multiple `Estimate` observations accumulate over real elapsed AlphaLab
   refresh time, and which feeds `alpha_lab.screener.service`'s
   `analyst_revisions` SCORING category. Nothing in this module touches
   that category, `Estimate`, or `StockResearch.overall_score` -- these are
   supplemental research evidence only, exactly like Analyst Consensus /
   Technical Summary / AI Research Rating.

Read methods here are pure database reads (no provider, no computation) --
safe to call from a Streamlit render. Refresh methods call a provider and
are meant to be triggered explicitly (a script or a UI button), never from
an ordinary page render. A failed refresh leaves previously stored rows
untouched -- both tables are append-only, so there is nothing to overwrite.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import AnalystRatingChange, EstimateRevisionTrend
from alpha_lab.ingestion.analyst_events import snapshot_analyst_rating_changes
from alpha_lab.ingestion.estimate_revisions import snapshot_estimate_revisions
from alpha_lab.providers.errors import ProviderError
from alpha_lab.providers.interfaces import AnalystEventProvider
from alpha_lab.research.analyst_research import (
    AnalystResearchSummary,
    build_analyst_research_summary,
)

# Generous cap on rating-change rows fetched for summarization -- far more
# than any real ticker could accumulate within RATING_CHANGE_WINDOW_DAYS,
# so the 90-day tally is never silently truncated, while still avoiding an
# unbounded full-history fetch on every research read.
_SUMMARY_RATING_CHANGES_FETCH_LIMIT = 200


class AnalystEventsService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation ------------------------

    def get_rating_changes(
        self, ticker: str, *, limit: int | None = 20
    ) -> list[AnalystRatingChange]:
        """Most recent rating-change events first. `limit=None` returns the
        full stored history."""
        normalized = ticker.strip().upper()
        with Session(self.engine) as session:
            statement = (
                select(AnalystRatingChange)
                .where(AnalystRatingChange.ticker == normalized)
                .order_by(AnalystRatingChange.grade_date.desc(), AnalystRatingChange.id.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            session.expunge_all()
            return list(rows)

    def get_latest_revision_trend(self, ticker: str) -> list[EstimateRevisionTrend]:
        """The most recent stored observation for each `fiscal_period` --
        never a mix of an old and a newer observation for the same period,
        and never re-derived/interpolated between observations."""
        normalized = ticker.strip().upper()
        with Session(self.engine) as session:
            rows = session.scalars(
                select(EstimateRevisionTrend)
                .where(EstimateRevisionTrend.ticker == normalized)
                .order_by(
                    EstimateRevisionTrend.fiscal_period,
                    EstimateRevisionTrend.observation_date.desc(),
                )
            ).all()
            session.expunge_all()
        latest_by_period: dict[date, EstimateRevisionTrend] = {}
        for row in rows:
            if row.fiscal_period not in latest_by_period:
                latest_by_period[row.fiscal_period] = row
        return [latest_by_period[period] for period in sorted(latest_by_period)]

    def get_research_summary(
        self, ticker: str, *, as_of: date | None = None
    ) -> AnalystResearchSummary | None:
        """The canonical, cross-layer Analyst Research summary (PR #26) for
        one ticker -- pure DB read plus deterministic computation, no
        provider call. Returns None when neither rating-change history nor
        revision trend data exists yet for this ticker."""
        rating_changes = self.get_rating_changes(
            ticker, limit=_SUMMARY_RATING_CHANGES_FETCH_LIMIT
        )
        revision_trend = self.get_latest_revision_trend(ticker)
        return build_analyst_research_summary(
            ticker, rating_changes, revision_trend, as_of=as_of or date.today()
        )

    # --- refreshes: explicit, provider calls happen before any DB write ---

    def refresh_rating_changes(self, ticker: str, provider: AnalystEventProvider) -> int:
        """Fetch the full rating-change history and persist any events not
        already stored. Raises ProviderError on failure, leaving
        previously stored events untouched."""
        symbol = ticker.strip().upper()
        events = provider.get_analyst_rating_changes(symbol)
        if not events:
            return 0
        return snapshot_analyst_rating_changes(
            self.engine,
            symbol,
            events,
            provider=provider.provider_name,
            source="yfinance upgradeDowngradeHistory",
        )

    def refresh_revision_trend(
        self, ticker: str, provider: AnalystEventProvider, *, as_of: date | None = None
    ) -> int:
        """Fetch + persist one estimate-revision-trend observation per
        ticker. Raises ProviderError on failure, leaving previously stored
        observations untouched."""
        symbol = ticker.strip().upper()
        observation_date = as_of or date.today()
        observations = provider.get_estimate_revision_trend(symbol, observation_date)
        if not observations:
            return 0
        return snapshot_estimate_revisions(
            self.engine,
            symbol,
            observation_date,
            observations,
            provider=provider.provider_name,
            source="yfinance earningsTrend",
        )

    def refresh_all(self, ticker: str, provider: AnalystEventProvider) -> "AnalystEventsRefreshOutcome":
        """Both new evidence layers, each attempted independently -- a
        failure in one never blocks or is masked by the other. A partial
        failure is returned in the outcome's `*_error` fields rather than
        raised, since a caller (e.g. a single "Refresh" button) needs both
        results to report accurately."""
        rating_changes_stored = 0
        rating_changes_error: ProviderError | None = None
        try:
            rating_changes_stored = self.refresh_rating_changes(ticker, provider)
        except ProviderError as error:
            rating_changes_error = error

        revision_trend_stored = 0
        revision_trend_error: ProviderError | None = None
        try:
            revision_trend_stored = self.refresh_revision_trend(ticker, provider)
        except ProviderError as error:
            revision_trend_error = error

        return AnalystEventsRefreshOutcome(
            rating_changes_stored=rating_changes_stored,
            rating_changes_error=rating_changes_error,
            revision_trend_stored=revision_trend_stored,
            revision_trend_error=revision_trend_error,
        )


@dataclass
class AnalystEventsRefreshOutcome:
    """Combined outcome of refreshing both new evidence layers for one
    ticker in a single UI action -- explicit per-layer counts and errors,
    never a single opaque success/failure."""

    rating_changes_stored: int
    rating_changes_error: ProviderError | None
    revision_trend_stored: int
    revision_trend_error: ProviderError | None
