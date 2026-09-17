"""Canonical Analyst Research summary (PR #26): the read-model layer that
turns raw `AnalystRatingChange`/`EstimateRevisionTrend` rows (see
`alpha_lab.research.analyst_events`'s module docstring for the full
four-layer analyst-evidence map) into an inspectable, per-ticker summary --
"what are analysts changing, and are their expectations moving", alongside
the pre-existing "what do analysts currently think" (`AnalystConsensus`).

Pure and deterministic: takes already-fetched ORM rows, never calls a
provider or touches the database. Never collapses the underlying evidence
into one opaque analyst score -- `AnalystResearchSummary` exposes the
recent rating-change events, a 90-day upgrade/downgrade tally, and a
per-fiscal-period revision trend (with an honest `RevisionDirection`, never
forced to a value when the underlying data is insufficient) as separate,
traceable fields. Never touches `alpha_lab.ratings.estimates.
calculate_revision_factors`, the `analyst_revisions` scoring category, or
`StockResearch.overall_score`.
"""

from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from alpha_lab.database.models import AnalystRatingChange, EstimateRevisionTrend

ANALYST_RESEARCH_METHODOLOGY_VERSION = "analyst-research-v1"

# Trailing window for the rating-change tally. Not a scoring parameter --
# purely a display/evidence-summarization choice, versioned alongside the
# rest of this module's methodology.
RATING_CHANGE_WINDOW_DAYS = 90

_UPGRADE_ACTIONS = frozenset({"up"})
_DOWNGRADE_ACTIONS = frozenset({"down"})
_INITIATION_ACTIONS = frozenset({"init"})
_REITERATION_ACTIONS = frozenset({"main", "reit"})


class RevisionDirection(StrEnum):
    IMPROVING = "IMPROVING"
    DETERIORATING = "DETERIORATING"
    STABLE = "STABLE"
    # Insufficient data to determine a direction (current or 30-day-ago
    # value missing) -- never guessed, never defaulted to STABLE.
    REVIEW = "REVIEW"


class RatingChangeSummary(BaseModel):
    grade_date: date
    firm: str | None
    to_grade: str | None
    from_grade: str | None
    action: str | None
    price_target_action: str | None
    current_price_target: float | None
    prior_price_target: float | None


class RevisionTrendPeriod(BaseModel):
    fiscal_period: date
    eps_trend_current: float | None
    eps_trend_7d_ago: float | None
    eps_trend_30d_ago: float | None
    eps_trend_60d_ago: float | None
    eps_trend_90d_ago: float | None
    revisions_up_last_7d: int | None
    revisions_up_last_30d: int | None
    revisions_down_last_7d: int | None
    revisions_down_last_30d: int | None
    # Derived from eps_trend_current vs. eps_trend_30d_ago -- REVIEW (never
    # a guessed direction) when either is missing.
    direction: RevisionDirection
    observation_date: date


class AnalystResearchSummary(BaseModel):
    ticker: str
    # Most recent rating-change events (bounded, see
    # `build_analyst_research_summary`'s `recent_changes_limit`). Empty when
    # this ticker genuinely has no rating-change history refreshed yet or
    # confirmed no coverage (e.g. most ETFs) -- see
    # `alpha_lab.research.analyst_events`'s "no coverage vs never refreshed"
    # note; this object does not attempt to disambiguate the two.
    recent_rating_changes: list[RatingChangeSummary]
    # None (never a fabricated zero) when `recent_rating_changes` reflects
    # no rating-change history at all; a real dict with zero counts is a
    # genuine "no changes in this window" fact once history exists.
    rating_change_counts_90d: dict[str, int] | None
    revision_trend: list[RevisionTrendPeriod]
    # 0.0/0.5/1.0 across the two sub-domains (rating changes, revision
    # trend) actually present -- mirrors AIEvidenceCoverage's domain-aware
    # pattern (absent counts as 0, never excluded from the denominator).
    coverage: float = Field(ge=0, le=1)
    as_of: date
    methodology_version: str = ANALYST_RESEARCH_METHODOLOGY_VERSION
    # Namespaced evidence IDs (analyst_rating_change:<id>,
    # estimate_revision_trend:<id>) tracing this summary back to the exact
    # underlying rows it was built from.
    evidence_ids: list[str]


def _summarize_change(row: AnalystRatingChange) -> RatingChangeSummary:
    return RatingChangeSummary(
        grade_date=row.grade_date.date(),
        firm=row.firm,
        to_grade=row.to_grade,
        from_grade=row.from_grade,
        action=row.action,
        price_target_action=row.price_target_action,
        current_price_target=row.current_price_target,
        prior_price_target=row.prior_price_target,
    )


def _compute_direction(current: float | None, prior_30d: float | None) -> RevisionDirection:
    if current is None or prior_30d is None:
        return RevisionDirection.REVIEW
    if current > prior_30d:
        return RevisionDirection.IMPROVING
    if current < prior_30d:
        return RevisionDirection.DETERIORATING
    return RevisionDirection.STABLE


def _summarize_trend_period(row: EstimateRevisionTrend) -> RevisionTrendPeriod:
    return RevisionTrendPeriod(
        fiscal_period=row.fiscal_period,
        eps_trend_current=row.eps_trend_current,
        eps_trend_7d_ago=row.eps_trend_7d_ago,
        eps_trend_30d_ago=row.eps_trend_30d_ago,
        eps_trend_60d_ago=row.eps_trend_60d_ago,
        eps_trend_90d_ago=row.eps_trend_90d_ago,
        revisions_up_last_7d=row.revisions_up_last_7d,
        revisions_up_last_30d=row.revisions_up_last_30d,
        revisions_down_last_7d=row.revisions_down_last_7d,
        revisions_down_last_30d=row.revisions_down_last_30d,
        direction=_compute_direction(row.eps_trend_current, row.eps_trend_30d_ago),
        observation_date=row.observation_date,
    )


def build_analyst_research_summary(
    ticker: str,
    rating_changes: list[AnalystRatingChange],
    revision_trend: list[EstimateRevisionTrend],
    *,
    as_of: date,
    recent_changes_limit: int = 10,
) -> AnalystResearchSummary | None:
    """Pure construction from already-fetched rows. Returns None (never an
    empty-but-present object) when NEITHER domain has any data at all --
    matching `StockResearch.analyst_consensus`/`technical_summary`'s "None
    means not computed for this research state" convention.

    `rating_changes` is expected most-recent-first (as returned by
    `AnalystEventsService.get_rating_changes`); `revision_trend` is expected
    one row per fiscal period, already the latest observation for that
    period (as returned by `AnalystEventsService.get_latest_revision_trend`).
    """
    if not rating_changes and not revision_trend:
        return None

    window_start = as_of - timedelta(days=RATING_CHANGE_WINDOW_DAYS)
    windowed = [change for change in rating_changes if change.grade_date.date() >= window_start]
    rating_change_counts_90d = (
        {
            "upgrades": sum(1 for c in windowed if c.action in _UPGRADE_ACTIONS),
            "downgrades": sum(1 for c in windowed if c.action in _DOWNGRADE_ACTIONS),
            "initiations": sum(1 for c in windowed if c.action in _INITIATION_ACTIONS),
            "reiterations": sum(1 for c in windowed if c.action in _REITERATION_ACTIONS),
        }
        if rating_changes
        else None
    )

    coverage = (0.5 if rating_changes else 0.0) + (0.5 if revision_trend else 0.0)

    evidence_ids = [
        f"analyst_rating_change:{change.id}" for change in rating_changes[:recent_changes_limit]
    ] + [f"estimate_revision_trend:{period.id}" for period in revision_trend]

    return AnalystResearchSummary(
        ticker=ticker.upper(),
        recent_rating_changes=[
            _summarize_change(change) for change in rating_changes[:recent_changes_limit]
        ],
        rating_change_counts_90d=rating_change_counts_90d,
        revision_trend=[_summarize_trend_period(period) for period in revision_trend],
        coverage=coverage,
        as_of=as_of,
        evidence_ids=evidence_ids,
    )
