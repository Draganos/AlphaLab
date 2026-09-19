"""Research Stance (PR #31): a cross-domain synthesis of AlphaLab's
already-computed research evidence into one inspectable, traceable summary.

Deliberately its own top-level package, not part of `alpha_lab.research`:
this module reads Macro Regime / External Calibration (via
`alpha_lab.alignment.AlignmentAssessment`) and the News Engine, both of
which `tests/test_macro_regression.py`/`test_news_regression.py` forbid any
`alpha_lab.research`/`.screener`/`.strategy`/`.backtest`/`.portfolio`/
`.ratings`/`.factors` module from importing -- the same reasoning
`alpha_lab.evidence_coverage`/`alpha_lab.alignment` already follow. This
module is read-only and imported by nothing in those scoring modules; it
may freely import `alpha_lab.research` types (the reverse direction), the
same way `alpha_lab.evidence_coverage` does.

Per the project roadmap's explicit PR #31 requirements:
  * Do NOT replace the fundamental score. Nothing here writes to or reads
    back into `StockResearch.overall_score`/`categories`, and this module
    computes no new 0-100 score of its own.
  * Do NOT create an arbitrary weighted average. `ResearchStanceOutcome`
    is an unweighted, deterministic count of how many already-categorical
    domains lean positive vs. negative -- never a blend of magnitudes, and
    a tie is its own explicit outcome rather than being split one way.
  * Every conclusion must be traceable to underlying evidence. Every
    `StanceLine.label` is the domain's OWN existing categorical vocabulary
    (`AnalystRating`, `TechnicalRating`, `RevisionDirection`,
    `AIDimensionValue`, `MacroRegime`, `DonatienLean`, or
    `StockResearch.score_interpretation`), verbatim -- this module invents
    no new user-facing terminology, only reads what each domain already
    concluded.
  * If evidence conflicts, expose the conflict. `ResearchStance.conflicts`
    lists every detected disagreement in plain English; `primary_conflict`
    names the single most salient one. Neither is ever hidden inside
    `outcome`, which stays a coarse categorical label alongside them, not
    a replacement for reading the actual lines.

Two of the nine domains the roadmap lists as potential inputs -- News and
Coverage/confidence -- are deliberately non-directional here and never
participate in `outcome`/`conflicts`:
  * News (`alpha_lab.news`) has, by its own module docstring, "no
    sentiment, no relevance, no derived judgment of any kind" -- there is
    no existing categorical vocabulary to read, and inventing one now
    would be exactly the kind of fabricated conclusion this project
    refuses to produce. It is surfaced here purely as an evidence-count
    fact (`StanceLine.detail`), same as `alpha_lab.evidence_coverage`'s
    own presence-only treatment of News.
  * Coverage/confidence (`StockResearch.confidence`/`confidence_label`) is
    a meta-statement about how much evidence exists across every other
    domain, not itself a directional opinion about the security -- folding
    it into the positive/negative tally would double-count the very
    domains it is describing.
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from alpha_lab.alignment.alignment import AlignmentAssessment, DonatienLean
from alpha_lab.database.models import NewsArticleRecord
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research.ai_rating import AIDimensionValue, AIResearchAssessment
from alpha_lab.research.analyst_consensus import AnalystConsensus, AnalystRating
from alpha_lab.research.analyst_research import AnalystResearchSummary, RevisionDirection
from alpha_lab.research.model import StockResearch
from alpha_lab.research.technical import TechnicalRating, TechnicalSummary

RESEARCH_STANCE_METHODOLOGY_VERSION = "research-stance-v1"

# Fixed evaluation order for every list this module produces (domain lines,
# conflict detection, the primary-conflict pick) -- never re-sorted by
# label/lean, so the same inputs always produce byte-identical output and
# "primary_conflict" is a deterministic, reproducible choice rather than
# whichever conflict happened to be found first by accident of dict order.
DOMAIN_ORDER: tuple[str, ...] = (
    "fundamentals",
    "analysts",
    "revisions",
    "technical",
    "ai_research",
    "macro",
    "external_calibration",
)

DOMAIN_LABELS: dict[str, str] = {
    "fundamentals": "Fundamentals",
    "analysts": "Analyst Consensus",
    "revisions": "Analyst Revisions",
    "technical": "Technical",
    "ai_research": "AI Research",
    "macro": "Macro Regime",
    "external_calibration": "External Calibration",
    "news": "News",
    "coverage_confidence": "Evidence Confidence",
}


class StanceLean(StrEnum):
    """Internal-only 4-way bucket used solely to detect cross-domain
    agreement/conflict for `ResearchStance.outcome`/`conflicts` -- never
    displayed on its own. What's actually shown is always
    `StanceLine.label`, the domain's own native vocabulary verbatim; this
    enum exists only so domains with different vocabularies (BUY vs.
    IMPROVING vs. RISK_ON) can be compared for directional agreement at
    all, without inventing a shared numeric scale."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    # This domain could in principle be directional, but the evidence
    # available right now doesn't support a lean -- never guessed, and
    # never counted toward `outcome`/`conflicts`. Distinct from a
    # non-directional domain (News, Coverage/confidence), which never has
    # a lean of any kind (see `StanceLine.lean`'s docstring).
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ResearchStanceOutcome(StrEnum):
    """Deterministic, unweighted summary of the directional domains'
    leans -- a count, never a blended score. See `_determine_outcome`."""

    POSITIVE = "POSITIVE"
    MIXED_POSITIVE = "MIXED_POSITIVE"
    NEUTRAL = "NEUTRAL"
    MIXED_NEGATIVE = "MIXED_NEGATIVE"
    NEGATIVE = "NEGATIVE"
    # No directional domain has a definite (POSITIVE/NEGATIVE) lean at all
    # -- never forced to NEUTRAL, which would misrepresent "no basis to
    # judge" as "judged and balanced".
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class StanceLine(BaseModel):
    domain: str
    label_display: str
    # The domain's own existing categorical value, verbatim (e.g. "BUY",
    # "IMPROVING", "Strong", "RISK_ON") -- what a UI actually renders.
    label: str
    # None for a structurally non-directional domain (News,
    # Coverage/confidence) -- this is not "insufficient evidence", it is
    # "this domain does not express a positive/negative opinion at all",
    # the same NOT_APPLICABLE-vs-UNAVAILABLE distinction
    # `alpha_lab.research.security_type` already draws elsewhere in this
    # codebase. A directional domain with no usable evidence right now
    # gets `StanceLean.INSUFFICIENT_DATA` instead, never `None`.
    lean: StanceLean | None
    # Optional human-readable extra context that isn't itself a lean (e.g.
    # News's article count, Coverage/confidence's numeric coverage).
    detail: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ResearchStance(BaseModel):
    ticker: str
    as_of: date
    lines: list[StanceLine]
    outcome: ResearchStanceOutcome
    # Every detected disagreement between a positive-leaning and a
    # negative-leaning directional domain, plain English, in `DOMAIN_ORDER`
    # -- nothing here is ever hidden inside `outcome` alone.
    conflicts: list[str]
    # The single most salient conflict (conflicts[0] if any), or None.
    # Convenience only -- `conflicts` is the complete, authoritative list.
    primary_conflict: str | None
    methodology_version: str = RESEARCH_STANCE_METHODOLOGY_VERSION


def _strip_provisional(label: str) -> str:
    return label.removeprefix("Provisional — ")


def _fundamentals_lean(score_interpretation: str) -> StanceLean:
    """`score_interpretation` is `StockResearch`'s own existing
    `alpha_lab.strategy.scoring.coverage_interpretation` output --
    "Exceptional candidate"/"Strong"/"Positive"/"Neutral"/"Weak"/
    "Avoid/review", optionally prefixed "Provisional — ", or "Insufficient
    data"/"Unavailable". This function only buckets that existing string
    for internal conflict detection; the label shown is the string itself,
    unmodified."""
    label = _strip_provisional(score_interpretation)
    if label in ("Exceptional candidate", "Strong", "Positive"):
        return StanceLean.POSITIVE
    if label == "Neutral":
        return StanceLean.NEUTRAL
    if label in ("Weak", "Avoid/review"):
        return StanceLean.NEGATIVE
    return StanceLean.INSUFFICIENT_DATA  # "Insufficient data" / "Unavailable"


def _build_fundamentals_line(research: StockResearch) -> StanceLine:
    label = research.score_interpretation
    return StanceLine(
        domain="fundamentals",
        label_display=DOMAIN_LABELS["fundamentals"],
        label=label,
        lean=_fundamentals_lean(label),
        detail=(
            f"overall score {research.overall_score:.0f}/100"
            if research.overall_score is not None
            else None
        ),
        evidence_ids=["fundamentals:overall_score"],
    )


def _build_analysts_line(analyst_consensus: AnalystConsensus | None) -> StanceLine:
    # `AnalystConsensus.rating` is declared `AnalystRating | None` on the
    # model (defaults to `None`) even though `build_analyst_consensus`
    # itself never produces a bare `None` (REVIEW covers "couldn't be
    # rated") -- guarded the same way `_build_macro_line`/`_build_
    # external_calibration_line` guard their own Optional sub-fields,
    # rather than trusting every construction path to match the one
    # production builder.
    if analyst_consensus is None or analyst_consensus.rating is None:
        return StanceLine(
            domain="analysts", label_display=DOMAIN_LABELS["analysts"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    lean = {
        AnalystRating.STRONG_BUY: StanceLean.POSITIVE,
        AnalystRating.BUY: StanceLean.POSITIVE,
        AnalystRating.NEUTRAL: StanceLean.NEUTRAL,
        AnalystRating.SELL: StanceLean.NEGATIVE,
        AnalystRating.STRONG_SELL: StanceLean.NEGATIVE,
        AnalystRating.REVIEW: StanceLean.INSUFFICIENT_DATA,
    }[analyst_consensus.rating]
    return StanceLine(
        domain="analysts", label_display=DOMAIN_LABELS["analysts"],
        label=analyst_consensus.rating.value, lean=lean,
        detail=(
            f"{analyst_consensus.total_analysts} analyst(s)"
            if analyst_consensus.total_analysts is not None
            else None
        ),
        evidence_ids=["analyst_consensus:rating"],
    )


def _build_revisions_line(analyst_research: AnalystResearchSummary | None) -> StanceLine:
    """Uses the NEAREST fiscal period's `direction` --
    `AnalystResearchSummary.revision_trend` is sorted ascending by
    `fiscal_period` (see `AnalystEventsService.get_latest_revision_trend`),
    so `revision_trend[0]` is the soonest period analysts are currently
    estimating, the most decision-relevant single period for a one-line
    stance read. This deliberately does not attempt to summarize every
    period into one direction -- the full list remains available on
    `StockResearch.analyst_research.revision_trend` for anyone who wants
    it."""
    if analyst_research is None or not analyst_research.revision_trend:
        return StanceLine(
            domain="revisions", label_display=DOMAIN_LABELS["revisions"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    nearest = analyst_research.revision_trend[0]
    lean = {
        RevisionDirection.IMPROVING: StanceLean.POSITIVE,
        RevisionDirection.STABLE: StanceLean.NEUTRAL,
        RevisionDirection.DETERIORATING: StanceLean.NEGATIVE,
        RevisionDirection.REVIEW: StanceLean.INSUFFICIENT_DATA,
    }[nearest.direction]
    return StanceLine(
        domain="revisions", label_display=DOMAIN_LABELS["revisions"],
        label=nearest.direction.value, lean=lean,
        detail=f"fiscal period {nearest.fiscal_period.isoformat()}",
        evidence_ids=["analyst_research:revision_trend[0]"],
    )


def _build_technical_line(technical_summary: TechnicalSummary | None) -> StanceLine:
    if technical_summary is None:
        return StanceLine(
            domain="technical", label_display=DOMAIN_LABELS["technical"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    lean = {
        TechnicalRating.STRONG_BUY: StanceLean.POSITIVE,
        TechnicalRating.BUY: StanceLean.POSITIVE,
        TechnicalRating.NEUTRAL: StanceLean.NEUTRAL,
        TechnicalRating.SELL: StanceLean.NEGATIVE,
        TechnicalRating.STRONG_SELL: StanceLean.NEGATIVE,
        TechnicalRating.REVIEW: StanceLean.INSUFFICIENT_DATA,
    }[technical_summary.overall_rating]
    return StanceLine(
        domain="technical", label_display=DOMAIN_LABELS["technical"],
        label=technical_summary.overall_rating.value, lean=lean,
        evidence_ids=["technical_summary:overall_rating"],
    )


def _build_ai_research_line(ai_research_assessment: AIResearchAssessment | None) -> StanceLine:
    if ai_research_assessment is None:
        return StanceLine(
            domain="ai_research", label_display=DOMAIN_LABELS["ai_research"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    lean = {
        AIDimensionValue.VERY_POSITIVE: StanceLean.POSITIVE,
        AIDimensionValue.POSITIVE: StanceLean.POSITIVE,
        AIDimensionValue.NEUTRAL: StanceLean.NEUTRAL,
        AIDimensionValue.NEGATIVE: StanceLean.NEGATIVE,
        AIDimensionValue.VERY_NEGATIVE: StanceLean.NEGATIVE,
        AIDimensionValue.REVIEW: StanceLean.INSUFFICIENT_DATA,
    }[ai_research_assessment.rating]
    return StanceLine(
        domain="ai_research", label_display=DOMAIN_LABELS["ai_research"],
        label=ai_research_assessment.rating.value, lean=lean,
        evidence_ids=["ai_research_assessment:rating"],
    )


def _build_macro_line(alignment: AlignmentAssessment | None) -> StanceLine:
    """`alignment` is an `alpha_lab.alignment.AlignmentAssessment` -- already
    a point-in-time-safe read of Macro Regime, computed and persisted
    elsewhere. Market-wide, not security-specific: this line reads
    identically for every ticker evaluated against the same `alignment`."""
    if alignment is None or alignment.market_regime is None:
        return StanceLine(
            domain="macro", label_display=DOMAIN_LABELS["macro"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    lean = {
        MacroRegime.RISK_ON: StanceLean.POSITIVE,
        MacroRegime.NEUTRAL: StanceLean.NEUTRAL,
        MacroRegime.RISK_OFF: StanceLean.NEGATIVE,
        MacroRegime.REVIEW: StanceLean.INSUFFICIENT_DATA,
    }[alignment.market_regime]
    return StanceLine(
        domain="macro", label_display=DOMAIN_LABELS["macro"],
        label=alignment.market_regime.value, lean=lean,
        evidence_ids=["alignment:market_regime"],
    )


def _build_external_calibration_line(alignment: AlignmentAssessment | None) -> StanceLine:
    """`alignment.donatien_lean` -- see `_build_macro_line`; same
    market-wide, already-computed, point-in-time-safe source."""
    if alignment is None or alignment.donatien_lean is None:
        return StanceLine(
            domain="external_calibration", label_display=DOMAIN_LABELS["external_calibration"],
            label="NOT_COMPUTED", lean=StanceLean.INSUFFICIENT_DATA,
        )
    lean = {
        DonatienLean.CONSTRUCTIVE: StanceLean.POSITIVE,
        DonatienLean.MIXED: StanceLean.NEUTRAL,
        DonatienLean.DEFENSIVE: StanceLean.NEGATIVE,
        DonatienLean.UNKNOWN: StanceLean.INSUFFICIENT_DATA,
    }[alignment.donatien_lean]
    return StanceLine(
        domain="external_calibration", label_display=DOMAIN_LABELS["external_calibration"],
        label=alignment.donatien_lean.value, lean=lean,
        evidence_ids=["alignment:donatien_lean"],
    )


def _build_news_line(news_articles: list[NewsArticleRecord] | None) -> StanceLine:
    """Never directional -- see module docstring. `news_articles` is
    whatever window the caller already fetched (e.g.
    `NewsService.get_history(ticker, since=...)`); this function does not
    apply its own windowing."""
    if news_articles is None:
        return StanceLine(
            domain="news", label_display=DOMAIN_LABELS["news"],
            label="NOT_COMPUTED", lean=None,
        )
    count = len(news_articles)
    return StanceLine(
        domain="news", label_display=DOMAIN_LABELS["news"],
        label="NO_EVIDENCE" if count == 0 else "PRESENT",
        lean=None,
        detail=f"{count} article(s)",
    )


def _build_coverage_confidence_line(research: StockResearch) -> StanceLine:
    """Never directional -- see module docstring. Reuses
    `StockResearch.confidence_label`/`confidence`/`overall_coverage`
    verbatim; computes nothing new."""
    return StanceLine(
        domain="coverage_confidence", label_display=DOMAIN_LABELS["coverage_confidence"],
        label=research.confidence_label, lean=None,
        detail=(
            f"confidence {research.confidence:.1f}/10, "
            f"coverage {research.overall_coverage:.0%}"
        ),
        evidence_ids=["stock_research:confidence", "stock_research:overall_coverage"],
    )


def _determine_outcome(lines: list[StanceLine]) -> ResearchStanceOutcome:
    positive = [line for line in lines if line.lean is StanceLean.POSITIVE]
    negative = [line for line in lines if line.lean is StanceLean.NEGATIVE]
    if not positive and not negative:
        return ResearchStanceOutcome.INSUFFICIENT_DATA
    if not negative:
        return ResearchStanceOutcome.POSITIVE
    if not positive:
        return ResearchStanceOutcome.NEGATIVE
    if len(positive) > len(negative):
        return ResearchStanceOutcome.MIXED_POSITIVE
    if len(negative) > len(positive):
        return ResearchStanceOutcome.MIXED_NEGATIVE
    return ResearchStanceOutcome.NEUTRAL


def _detect_conflicts(lines: list[StanceLine]) -> list[str]:
    """Every disagreement between the minority lean and the majority lean
    among directional domains with a definite (POSITIVE/NEGATIVE) reading
    -- NEUTRAL and INSUFFICIENT_DATA domains never participate (there is
    nothing to disagree about). Order follows `DOMAIN_ORDER`, so the result
    is deterministic and reproducible from the same inputs."""
    by_domain = {line.domain: line for line in lines}
    positive = [d for d in DOMAIN_ORDER if by_domain[d].lean is StanceLean.POSITIVE]
    negative = [d for d in DOMAIN_ORDER if by_domain[d].lean is StanceLean.NEGATIVE]
    if not positive or not negative:
        return []
    majority, minority = (positive, negative) if len(positive) >= len(negative) else (negative, positive)
    majority_word = "positive" if majority is positive else "negative"
    majority_names = "/".join(DOMAIN_LABELS[d] for d in majority)
    return [
        f"{DOMAIN_LABELS[d]} ({by_domain[d].label}) disagrees with {majority_word} "
        f"evidence from {majority_names}"
        for d in minority
    ]


def build_research_stance(
    research: StockResearch,
    *,
    news_articles: list[NewsArticleRecord] | None = None,
    alignment: AlignmentAssessment | None = None,
) -> ResearchStance:
    """Pure construction from already-computed objects -- no provider call,
    no database read, no new scoring. `news_articles`/`alignment` are
    optional because those domains live outside `StockResearch` by design
    (mirrors `alpha_lab.evidence_coverage.build_security_coverage_summary`'s
    identical `news_articles`/`macro_assessment` pattern); omit them to get
    a stance built from whatever `StockResearch` itself already carries."""
    lines = [
        _build_fundamentals_line(research),
        _build_analysts_line(research.analyst_consensus),
        _build_revisions_line(research.analyst_research),
        _build_technical_line(research.technical_summary),
        _build_ai_research_line(research.ai_research_assessment),
        _build_macro_line(alignment),
        _build_external_calibration_line(alignment),
        _build_news_line(news_articles),
        _build_coverage_confidence_line(research),
    ]
    conflicts = _detect_conflicts(lines)
    return ResearchStance(
        ticker=research.ticker,
        as_of=research.evaluation_date,
        lines=lines,
        outcome=_determine_outcome(lines),
        conflicts=conflicts,
        primary_conflict=conflicts[0] if conflicts else None,
    )
