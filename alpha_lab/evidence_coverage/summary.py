"""Research Evidence & Coverage Dashboard (PR #28): a pure, read-only
summarization layer that combines several independent evidence domains --
`StockResearch`'s fundamental categories, Analyst Consensus, Analyst
Research (rating changes + revision trend), Technical Summary, AI Research
Rating, the News Engine, and Macro Regime -- into one per-security coverage
view, without collapsing them into a new score.

Deliberately its own top-level package, not part of `alpha_lab.research`:
the News Engine and Macro Regime modules each declare that nothing in
`alpha_lab.research`/`.screener`/`.strategy`/`.backtest`/`.portfolio`/
`.ratings`/`.factors` (the scoring/ranking modules) may import them, so a
combining layer that legitimately depends on both sides has to live
outside that boundary -- the same reasoning `alpha_lab.alignment` (Donatien
<-> Macro Regime) already follows. This module is read-only and imported by
nothing in those scoring modules; it may freely import `alpha_lab.research`
types (the reverse direction) the same way `alpha_lab.alignment` imports
`alpha_lab.macro`.

This module answers "how much evidence do I actually have for this
security?" without computing anything new: every coverage / evidence-count /
freshness figure here is read directly off a value that already exists
somewhere else in the codebase. It never produces a second composite/overall
score, and it never fabricates a `limitation_reason` finer than the
underlying data supports.

Deliberately NOT distinguished here: "insufficient history" vs "provider
failure" vs "confirmed unavailable" for individual fundamental metrics --
`alpha_lab.research.model`'s own docstring already flags that
`MetricStatus.INVALID` is reserved but unwired, and that today's coercion
helpers collapse every non-finite/missing input to the same `None`. A
`CoverageRow.status`/`limitation_reason` pair here is only ever as precise
as that upstream data allows; see each field's docstring for exactly what
is knowable.
"""

from datetime import date, datetime
from enum import StrEnum

import pandas as pd
from pydantic import BaseModel, Field

from alpha_lab.database.models import NewsArticleRecord
from alpha_lab.macro.regime import MacroAssessment
from alpha_lab.research.ai_rating import (
    AI_MINIMUM_ASSESSABLE_DIMENSIONS,
    AI_MINIMUM_EVIDENCE_COVERAGE,
    AIDimensionValue,
    AIResearchAssessment,
)
from alpha_lab.research.analyst_consensus import AnalystConsensus
from alpha_lab.research.analyst_research import AnalystResearchSummary
from alpha_lab.research.model import CategoryStatus, StockResearch
from alpha_lab.research.technical import (
    MIN_COVERAGE_THRESHOLD,
    TOTAL_INDICATOR_COUNT,
    TechnicalSummary,
)

COVERAGE_SUMMARY_METHODOLOGY_VERSION = "coverage-summary-v1"


class CoverageStatus(StrEnum):
    """What can actually be told about one category's evidence state, no
    finer than the underlying coverage number supports."""

    FULL = "FULL"  # coverage == 1.0
    PARTIAL = "PARTIAL"  # 0 < coverage < 1.0
    NO_EVIDENCE = "NO_EVIDENCE"  # category was computed but coverage == 0
    # The underlying research object itself is None -- never computed for
    # this research state, distinct from "computed and found empty".
    NOT_COMPUTED = "NOT_COMPUTED"
    # PR #29: this category is structurally not applicable to this
    # security's type (e.g. business_quality for an ETF) -- never counted
    # against coverage as though it were missing evidence. Distinct from
    # NO_EVIDENCE (expected but absent) and NOT_COMPUTED (not yet computed
    # at all); see alpha_lab.research.security_type's module docstring.
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _status_for(coverage: float | None) -> CoverageStatus:
    if coverage is None:
        return CoverageStatus.NOT_COMPUTED
    if coverage >= 1.0:
        return CoverageStatus.FULL
    if coverage <= 0.0:
        return CoverageStatus.NO_EVIDENCE
    return CoverageStatus.PARTIAL


class CoverageRow(BaseModel):
    category: str
    label: str
    coverage: float | None = Field(None, ge=0, le=1)
    status: CoverageStatus
    evidence_count: int | None = Field(None, ge=0)
    freshness: date | datetime | None = None
    # Populated only when the underlying object exposes provider identity
    # at this layer -- empty means "not tracked here", never "no provider".
    providers: list[str] = Field(default_factory=list)
    limitation_reason: str | None = None


class SecurityCoverageSummary(BaseModel):
    ticker: str
    sector: str | None
    security_type: str | None
    evaluation_date: date
    rows: list[CoverageRow]
    methodology_version: str = COVERAGE_SUMMARY_METHODOLOGY_VERSION


def _fundamental_row(name: str, research: StockResearch) -> CoverageRow:
    category = research.categories[name]
    available_metrics = [m for m in category.metrics if m.status.value == "AVAILABLE"]
    retrieved_at_values = [m.retrieved_at for m in available_metrics if m.retrieved_at is not None]
    if category.status == CategoryStatus.NOT_APPLICABLE:
        # Structurally not applicable to this security's type -- never
        # rendered as though it were a coverage gap. See
        # alpha_lab.research.security_type's module docstring.
        return CoverageRow(
            category=name,
            label=category.label,
            coverage=category.coverage,
            status=CoverageStatus.NOT_APPLICABLE,
            evidence_count=len(available_metrics),
            freshness=max(retrieved_at_values) if retrieved_at_values else None,
            providers=sorted(set(category.sources)),
            limitation_reason="not applicable to this security type",
        )
    status = _status_for(category.coverage)
    reason = None
    if status is CoverageStatus.NO_EVIDENCE:
        reason = "no metrics available for this category"
    elif status is CoverageStatus.PARTIAL:
        reason = f"{len(category.unavailable_metrics)} metric(s) unavailable"
    return CoverageRow(
        category=name,
        label=category.label,
        coverage=category.coverage,
        status=status,
        evidence_count=len(available_metrics),
        freshness=max(retrieved_at_values) if retrieved_at_values else None,
        providers=sorted(set(category.sources)),
        limitation_reason=reason,
    )


def _analyst_consensus_row(analyst_consensus: AnalystConsensus | None) -> CoverageRow:
    coverage = None if analyst_consensus is None else analyst_consensus.coverage
    status = _status_for(coverage)
    reason = None
    if status is CoverageStatus.NOT_COMPUTED:
        reason = "not computed for this research state"
    elif status is CoverageStatus.NO_EVIDENCE:
        reason = "no analyst coverage confirmed for this security"
    elif status is CoverageStatus.PARTIAL:
        reason = "incomplete consensus fields reported by provider"
    return CoverageRow(
        category="analyst_consensus",
        label="Analyst Consensus",
        coverage=coverage,
        status=status,
        evidence_count=None if analyst_consensus is None else analyst_consensus.total_analysts,
        freshness=None if analyst_consensus is None else analyst_consensus.as_of,
        providers=[] if analyst_consensus is None else [analyst_consensus.source],
        limitation_reason=reason,
    )


def _analyst_history_row(analyst_research: AnalystResearchSummary | None) -> CoverageRow:
    if analyst_research is None:
        return CoverageRow(
            category="analyst_history",
            label="Analyst History",
            status=CoverageStatus.NOT_COMPUTED,
            limitation_reason="not computed for this research state",
        )
    has_history = bool(analyst_research.recent_rating_changes)
    coverage = 1.0 if has_history else 0.0
    status = _status_for(coverage)
    return CoverageRow(
        category="analyst_history",
        label="Analyst History",
        coverage=coverage,
        status=status,
        evidence_count=len(analyst_research.recent_rating_changes),
        freshness=analyst_research.recent_rating_changes[0].grade_date if has_history else None,
        limitation_reason=(
            None if has_history else "no rating-change history refreshed or confirmed no coverage"
        ),
    )


def _revisions_row(analyst_research: AnalystResearchSummary | None) -> CoverageRow:
    if analyst_research is None:
        return CoverageRow(
            category="revisions",
            label="Revisions",
            status=CoverageStatus.NOT_COMPUTED,
            limitation_reason="not computed for this research state",
        )
    has_trend = bool(analyst_research.revision_trend)
    coverage = 1.0 if has_trend else 0.0
    status = _status_for(coverage)
    freshness = (
        max(period.observation_date for period in analyst_research.revision_trend)
        if has_trend
        else None
    )
    return CoverageRow(
        category="revisions",
        label="Revisions",
        coverage=coverage,
        status=status,
        evidence_count=len(analyst_research.revision_trend),
        freshness=freshness,
        limitation_reason=(
            None if has_trend else "no EPS revision trend refreshed or confirmed no coverage"
        ),
    )


def _technical_row(technical_summary: TechnicalSummary | None) -> CoverageRow:
    coverage = None if technical_summary is None else technical_summary.coverage
    status = _status_for(coverage)
    evidence_count = (
        None
        if technical_summary is None
        else technical_summary.moving_average_available + technical_summary.oscillator_available
    )
    reason = None
    if status is CoverageStatus.NOT_COMPUTED:
        reason = "not computed for this research state"
    elif status is CoverageStatus.NO_EVIDENCE:
        reason = "no indicators computable (insufficient price history)"
    elif status is CoverageStatus.PARTIAL and coverage < MIN_COVERAGE_THRESHOLD:
        reason = f"below the {MIN_COVERAGE_THRESHOLD:.0%} minimum indicator coverage gate"
    elif status is CoverageStatus.PARTIAL:
        reason = f"{evidence_count}/{TOTAL_INDICATOR_COUNT} indicators available"
    return CoverageRow(
        category="technical",
        label="Technical",
        coverage=coverage,
        status=status,
        evidence_count=evidence_count,
        freshness=None if technical_summary is None else technical_summary.as_of,
        providers=[] if technical_summary is None else [technical_summary.source],
        limitation_reason=reason,
    )


def _ai_evidence_row(ai_research_assessment: AIResearchAssessment | None) -> CoverageRow:
    coverage = (
        None
        if ai_research_assessment is None
        else ai_research_assessment.evidence_coverage.overall_ai_evidence_coverage
    )
    status = _status_for(coverage)
    reason = None
    if status is CoverageStatus.NOT_COMPUTED:
        reason = "not computed for this research state"
    elif status is not CoverageStatus.FULL and ai_research_assessment is not None:
        assessable = sum(
            1
            for dimension in ai_research_assessment.dimensions.values()
            if dimension.value != AIDimensionValue.REVIEW
        )
        if coverage < AI_MINIMUM_EVIDENCE_COVERAGE:
            reason = f"below the {AI_MINIMUM_EVIDENCE_COVERAGE:.0%} AI minimum evidence-coverage gate"
        elif assessable < AI_MINIMUM_ASSESSABLE_DIMENSIONS:
            reason = (
                f"only {assessable}/{AI_MINIMUM_ASSESSABLE_DIMENSIONS} required "
                "dimensions assessable"
            )
        else:
            reason = f"{assessable}/{len(ai_research_assessment.dimensions)} AI dimensions assessable"
    return CoverageRow(
        category="ai_evidence",
        label="AI Evidence",
        coverage=coverage,
        status=status,
        evidence_count=(
            None if ai_research_assessment is None else len(ai_research_assessment.supporting_evidence)
        ),
        freshness=None if ai_research_assessment is None else ai_research_assessment.as_of,
        providers=[] if ai_research_assessment is None else [ai_research_assessment.source],
        limitation_reason=reason,
    )


def _news_row(news_articles: list[NewsArticleRecord] | None) -> CoverageRow:
    """`news_articles` is whatever `NewsService.get_history(ticker)` returns
    -- `None` means "not queried" (`NOT_COMPUTED`), an empty list means
    "queried, confirmed zero articles retrieved" (`NO_EVIDENCE`). Coverage
    here is deliberately presence-based (1.0/0.0), not a fine-grained
    percentage -- News has no canonical coverage baseline (see
    `alpha_lab.news.service`'s module docstring: a refresh can only ever
    capture news from the point it is run onward, never a historical
    archive), so a fabricated fractional number would misrepresent that."""
    if news_articles is None:
        return CoverageRow(
            category="news",
            label="News",
            status=CoverageStatus.NOT_COMPUTED,
            limitation_reason="not queried for this view",
        )
    coverage = 1.0 if news_articles else 0.0
    status = _status_for(coverage)
    return CoverageRow(
        category="news",
        label="News",
        coverage=coverage,
        status=status,
        evidence_count=len(news_articles),
        freshness=max(a.published_at for a in news_articles) if news_articles else None,
        providers=sorted({a.provider for a in news_articles}),
        limitation_reason=None if news_articles else "no news articles retrieved for this ticker yet",
    )


def _macro_row(macro_assessment: MacroAssessment | None) -> CoverageRow:
    """`macro_assessment` is an `alpha_lab.macro.regime.MacroAssessment` --
    market-wide, not security-specific (see that module's docstring), so
    this row reads identically for every security evaluated at the same
    time. `None` means "not queried" (`NOT_COMPUTED`)."""
    if macro_assessment is None:
        return CoverageRow(
            category="macro",
            label="Macro Regime (market-wide)",
            status=CoverageStatus.NOT_COMPUTED,
            limitation_reason="not queried for this view",
        )
    status = _status_for(macro_assessment.coverage)
    available = sum(1 for i in macro_assessment.indicators if i.signal is not None)
    reason = None
    if status is not CoverageStatus.FULL:
        reason = f"shared market-wide assessment; {available}/{len(macro_assessment.indicators)} indicators available"
    return CoverageRow(
        category="macro",
        label="Macro Regime (market-wide)",
        coverage=macro_assessment.coverage,
        status=status,
        evidence_count=available,
        freshness=macro_assessment.as_of,
        providers=[macro_assessment.source],
        limitation_reason=reason,
    )


def build_security_coverage_summary(
    research: StockResearch,
    *,
    news_articles: list[NewsArticleRecord] | None = None,
    macro_assessment: MacroAssessment | None = None,
) -> SecurityCoverageSummary:
    """Pure construction from already-fetched objects -- no provider call,
    no database read, no new scoring. `news_articles`/`macro_assessment`
    are optional because those two domains live outside `StockResearch` by
    design; omit them (leave `None`) to get a summary of everything
    `StockResearch` itself already carries."""
    rows = [_fundamental_row(name, research) for name in research.categories]
    rows.append(_analyst_consensus_row(research.analyst_consensus))
    rows.append(_analyst_history_row(research.analyst_research))
    rows.append(_revisions_row(research.analyst_research))
    rows.append(_technical_row(research.technical_summary))
    rows.append(_ai_evidence_row(research.ai_research_assessment))
    rows.append(_news_row(news_articles))
    rows.append(_macro_row(macro_assessment))
    return SecurityCoverageSummary(
        ticker=research.ticker,
        sector=research.sector,
        security_type=research.security_type,
        evaluation_date=research.evaluation_date,
        rows=rows,
    )


def flatten_coverage_rows(summaries: list[SecurityCoverageSummary]) -> list[dict]:
    """One flat dict per (security, category) pair -- exactly what a
    universe-wide breakdown (by security / category / security type /
    sector / provider) needs as input to a `pandas.DataFrame`/groupby, with
    no aggregation performed here. Pure reshaping only: every value is
    copied verbatim from the `SecurityCoverageSummary`/`CoverageRow` it
    came from, nothing is computed or inferred.

    A row's `providers` list is exploded into one flat dict per provider
    (or one dict with `provider=None` when the row tracks no provider at
    this layer) so a provider-level groupby sees each contributing provider
    exactly once rather than a stringified list.
    """
    flat: list[dict] = []
    for summary in summaries:
        for row in summary.rows:
            providers = row.providers or [None]
            for provider in providers:
                flat.append(
                    {
                        "ticker": summary.ticker,
                        "sector": summary.sector,
                        "security_type": summary.security_type,
                        "category": row.category,
                        "label": row.label,
                        "coverage": row.coverage,
                        "status": row.status.value,
                        "evidence_count": row.evidence_count,
                        "freshness": row.freshness,
                        "provider": provider,
                        "limitation_reason": row.limitation_reason,
                    }
                )
    return flat


_BREAKDOWN_GROUP_COLUMNS = frozenset({"category", "ticker", "security_type", "sector", "provider"})


def summarize_universe_breakdown(flat_rows: list[dict], group_by: str) -> list[dict]:
    """Aggregate `flatten_coverage_rows`' output by `group_by` (one of
    "category", "ticker", "security_type", "sector", "provider"): row
    count, mean coverage, and full/no-evidence/not-computed counts per
    group.

    `flatten_coverage_rows` deliberately explodes one `CoverageRow` into
    one flat dict per provider it cites -- correct input for a `provider`
    breakdown (each contributing provider counted once), but grouping by
    anything else directly on that same exploded data would double-count
    any (security, category) pair that happens to cite more than one
    provider. This function de-duplicates on (ticker, category) before
    grouping by anything other than `provider`, so `avg_coverage` and the
    status counts always reflect exactly one observation per security x
    category regardless of how many providers it cites.

    Sorted by `avg_coverage` ascending, weakest group first -- a group with
    no computed coverage at all (every row `NOT_COMPUTED`, `avg_coverage`
    is `NaN`) sorts first rather than last, consistent with how the
    Security Detail tab already orders `NOT_COMPUTED` ahead of `PARTIAL`/
    `FULL` (see `_STATUS_ORDER` in the dashboard page): the weakest
    evidence state leads either view, not just the lowest numeric value.
    The opposite NaN case -- every row in the group is `NOT_APPLICABLE`
    (e.g. "Valuation" grouped over a universe of nothing but ETFs) -- sorts
    last instead, alongside `FULL`: it is not evidence that's missing, and
    must never present as though it were the weakest group in the view.
    """
    if group_by not in _BREAKDOWN_GROUP_COLUMNS:
        raise ValueError(f"Unknown breakdown dimension: {group_by!r}")
    frame = pd.DataFrame(flat_rows)
    if frame.empty:
        return []
    source = frame if group_by == "provider" else frame.drop_duplicates(subset=["ticker", "category"])
    # PR #29: a NOT_APPLICABLE row's `coverage` is a real 0.0, not NaN like
    # NOT_COMPUTED's -- masked here so `avg_coverage` never counts a
    # structurally-inapplicable category against a group's average, the
    # same "not applicable != missing evidence" rule build.py already
    # applies to category_breadth/confidence.
    coverage_for_average = source["coverage"].where(
        source["status"] != CoverageStatus.NOT_APPLICABLE.value
    )
    grouped = (
        source.assign(_coverage_for_average=coverage_for_average)
        .groupby(group_by, dropna=False)
        .agg(
            rows=("category", "count"),
            avg_coverage=("_coverage_for_average", "mean"),
            full_coverage=("status", lambda s: (s == CoverageStatus.FULL.value).sum()),
            no_evidence=("status", lambda s: (s == CoverageStatus.NO_EVIDENCE.value).sum()),
            not_computed=("status", lambda s: (s == CoverageStatus.NOT_COMPUTED.value).sum()),
            not_applicable=("status", lambda s: (s == CoverageStatus.NOT_APPLICABLE.value).sum()),
        )
        .reset_index()
    )
    # A group whose every row is NOT_APPLICABLE has avg_coverage == NaN for
    # the same reason a NOT_COMPUTED-only group does, but the two must sort
    # oppositely -- push the former to +inf (sorts last, with FULL) instead
    # of leaving it NaN (which na_position="first" would float to the top).
    all_not_applicable = grouped["not_applicable"] == grouped["rows"]
    sort_key = grouped["avg_coverage"].mask(all_not_applicable, float("inf"))
    return grouped.assign(_sort_key=sort_key).sort_values(
        "_sort_key", ascending=True, na_position="first"
    ).drop(columns=["_sort_key"]).to_dict("records")
