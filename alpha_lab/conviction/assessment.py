"""Conviction / Conflict Layer (roadmap Phase 8): a deterministic, categorical
read of how independent evidence families agree or conflict for one security
at one evaluation moment. Pure computation over a `ResearchState` -- no I/O,
no provider, no database, no AI.

What this is NOT (each pinned by a test):
  * Not a score. There is no conviction number, weight, probability or
    average anywhere in the output; `FamilyAssessment.coverage` is measured
    evidence coverage, not conviction.
  * Not a vote count of domains. Agreement is counted across *independent
    evidence families*, so correlated domains cannot manufacture agreement:
      FUNDAMENTAL      the five fundamentals-only rating categories
                       (business quality, earnings growth, financial
                       strength, valuation, shareholder return)
      MARKET_OPINION   analyst consensus + analyst revisions (one family)
      PRICE_BEHAVIOUR  technicals
    Everything else is context or a cross-check and NEVER votes:
      regime           Macro Regime, Donatien lean (market-wide, identical for
                       every security -> can only conflict with the security)
      synthesis        the AI rating and the composite score (both are built
                       FROM the families above: the composite is 15%
                       revisions + 15% momentum + 10% AI, so letting it vote
                       would double count)
    News, ethics and coverage carry no direction.
  * Not an input to scoring, ranking, backtests or portfolios: nothing in
    those packages imports this one (static regression test).

Family sufficiency (reuses existing, configured thresholds; invents none):
  UNASSESSED   no lean: the domain's own label is REVIEW / insufficient /
               unavailable, or it is structurally not applicable (an ETF has
               no fundamentals categories), or family coverage is below
               `coverage.insufficient_below`. Listed with its reason.
               (Fundamental family coverage = the mean of its applicable
               categories' own evidence coverage.)
  PROVISIONAL  assessed but thin: coverage below `coverage.full_confidence_at`,
               newest price bar older than `data_quality.stale_price_days` (or
               its date not recorded),
               or a two-member family with one member unassessed.
  ESTABLISHED  everything else.
Only price staleness has a configured freshness policy today, so analyst and
fundamentals ages are shown, never gated on.

A PROVISIONAL family may join an agreement but cannot establish one, and a
conflict with a PROVISIONAL side is capped at SOFT.

Inside the Fundamental family the five categories are read with the system's
own `strategy.scoring.interpretation` labels (no new thresholds) and the
family lean is the sign of (#positive - #negative); an equal split is
NEUTRAL ("balanced") and the counts are always shown. This is the one
aggregation in the layer, it is a count not a magnitude average, and it is
visible in `FamilyAssessment.detail`.
"""

from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from alpha_lab.alignment.alignment import AlignmentAssessment
from alpha_lab.config.settings import CoverageSettings
from alpha_lab.data_quality import assess_freshness
from alpha_lab.research.model import CATEGORY_LABELS, CategoryStatus
from alpha_lab.research_stance import StanceLean, build_research_stance
from alpha_lab.strategy.scoring import interpretation

if TYPE_CHECKING:  # research_state imports this module; never import it back at runtime
    from alpha_lab.research_state import ResearchState

CONVICTION_METHODOLOGY_VERSION = "conviction-v1"

FUNDAMENTAL_CATEGORIES: tuple[str, ...] = (
    "business_quality",
    "earnings_growth",
    "financial_strength",
    "valuation",
    "shareholder_return",
)


class Family(StrEnum):
    FUNDAMENTAL = "FUNDAMENTAL"
    MARKET_OPINION = "MARKET_OPINION"
    PRICE_BEHAVIOUR = "PRICE_BEHAVIOUR"


FAMILY_ORDER: tuple[Family, ...] = (Family.FUNDAMENTAL, Family.MARKET_OPINION, Family.PRICE_BEHAVIOUR)
FAMILY_LABELS: dict[Family, str] = {
    Family.FUNDAMENTAL: "Fundamentals",
    Family.MARKET_OPINION: "Market opinion",
    Family.PRICE_BEHAVIOUR: "Price behaviour",
}


class FamilyStatus(StrEnum):
    ESTABLISHED = "ESTABLISHED"
    PROVISIONAL = "PROVISIONAL"
    UNASSESSED = "UNASSESSED"


class FamilyLean(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    # Market opinion only: its two members point opposite ways, so the
    # family casts no vote.
    CONFLICTED = "CONFLICTED"
    NONE = "NONE"  # unassessed


class ConflictKind(StrEnum):
    CROSS_FAMILY = "CROSS_FAMILY"
    INTRA_FAMILY = "INTRA_FAMILY"
    REGIME_VS_SECURITY = "REGIME_VS_SECURITY"
    SYNTHESIS_DIVERGENCE = "SYNTHESIS_DIVERGENCE"


class ConflictStrength(StrEnum):
    HARD = "HARD"  # both sides ESTABLISHED
    SOFT = "SOFT"  # at least one side PROVISIONAL
    CONTEXT = "CONTEXT"  # informational: a non-voting layer disagrees


class Agreement(StrEnum):
    CONVERGENT = "CONVERGENT"
    LEANING = "LEANING"
    CONTESTED = "CONTESTED"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT = "INSUFFICIENT"


class Direction(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NONE = "NONE"


class EvidenceSide(BaseModel):
    """One piece of evidence as a conflict/family cites it: the domain's own
    label verbatim, its research-state field status, and that field's
    provenance id."""

    domain: str
    label: str
    lean: str
    status: str
    provenance_id: str | None = None


class FamilyAssessment(BaseModel):
    family: Family
    status: FamilyStatus
    lean: FamilyLean
    reason: str
    members: list[EvidenceSide] = Field(default_factory=list)
    # Measured evidence coverage of the family (0..1), None when not
    # applicable. Never a conviction measure.
    coverage: float | None = None
    detail: str | None = None


class Conflict(BaseModel):
    kind: ConflictKind
    strength: ConflictStrength
    rule_id: str
    sides: list[EvidenceSide]
    explanation: str


class ConvictionAssessment(BaseModel):
    ticker: str
    evaluation_date: date
    methodology_version: str = CONVICTION_METHODOLOGY_VERSION
    direction: Direction
    agreement: Agreement
    families: list[FamilyAssessment]
    conflicts: list[Conflict]
    # Non-voting layers, shown for context (never counted).
    context: list[EvidenceSide] = Field(default_factory=list)
    synthesis: list[EvidenceSide] = Field(default_factory=list)


_SIGN = {FamilyLean.POSITIVE: Direction.POSITIVE, FamilyLean.NEGATIVE: Direction.NEGATIVE}


def _lean_from_label(label: str) -> StanceLean:
    if label in ("Exceptional candidate", "Strong", "Positive"):
        return StanceLean.POSITIVE
    if label == "Neutral":
        return StanceLean.NEUTRAL
    return StanceLean.NEGATIVE  # "Weak" / "Avoid/review"


def _family_lean(lean: StanceLean) -> FamilyLean:
    return {
        StanceLean.POSITIVE: FamilyLean.POSITIVE,
        StanceLean.NEGATIVE: FamilyLean.NEGATIVE,
        StanceLean.NEUTRAL: FamilyLean.NEUTRAL,
    }.get(lean, FamilyLean.NONE)


def _side(state: "ResearchState", field_name: str, domain: str, label: str, lean: str) -> EvidenceSide:
    field = getattr(state, field_name)
    return EvidenceSide(
        domain=domain, label=label, lean=lean, status=field.status, provenance_id=field.provenance_id
    )


def _unassessed(family: Family, reason: str, *, coverage: float | None = None) -> FamilyAssessment:
    return FamilyAssessment(
        family=family, status=FamilyStatus.UNASSESSED, lean=FamilyLean.NONE, reason=reason, coverage=coverage
    )


def _status_from_coverage(coverage: float, coverage_settings: CoverageSettings) -> FamilyStatus:
    if coverage < coverage_settings.insufficient_below:
        return FamilyStatus.UNASSESSED
    if coverage < coverage_settings.full_confidence_at:
        return FamilyStatus.PROVISIONAL
    return FamilyStatus.ESTABLISHED


def _fundamental_family(state: "ResearchState", coverage_settings: CoverageSettings) -> FamilyAssessment:
    research = state.stock_research
    if research is None:
        return _unassessed(Family.FUNDAMENTAL, "no research computed for this security")
    applicable = [
        research.categories[name]
        for name in FUNDAMENTAL_CATEGORIES
        if name in research.categories and research.categories[name].status is not CategoryStatus.NOT_APPLICABLE
    ]
    if not applicable:
        return _unassessed(Family.FUNDAMENTAL, "fundamentals categories are not applicable to this security type")
    scored = [category for category in applicable if category.score is not None]
    # Family coverage = the plain mean of the applicable categories' own
    # evidence coverage (metadata about how much evidence exists, the same
    # quantity `StockResearch.overall_coverage` reports; not a conviction
    # figure). A category with no evidence counts as 0.
    coverage = sum(category.coverage for category in applicable) / len(applicable)
    status = _status_from_coverage(coverage, coverage_settings)
    if status is FamilyStatus.UNASSESSED or not scored:
        return _unassessed(
            Family.FUNDAMENTAL,
            f"fundamentals evidence coverage {coverage:.0%}, {len(scored)}/{len(applicable)} categories scored "
            f"(minimum {coverage_settings.insufficient_below:.0%})",
            coverage=coverage,
        )
    leans = [(category, _lean_from_label(interpretation(category.score))) for category in scored]
    positive = sum(1 for _, lean in leans if lean is StanceLean.POSITIVE)
    negative = sum(1 for _, lean in leans if lean is StanceLean.NEGATIVE)
    neutral = len(leans) - positive - negative
    if positive > negative:
        lean = FamilyLean.POSITIVE
    elif negative > positive:
        lean = FamilyLean.NEGATIVE
    else:
        lean = FamilyLean.NEUTRAL
    detail = "; ".join(
        f"{CATEGORY_LABELS.get(category.name, category.name)} {category.score:.0f} ({interpretation(category.score)})"
        for category, _ in leans
    )
    label = f"{positive} positive / {negative} negative / {neutral} neutral"
    return FamilyAssessment(
        family=Family.FUNDAMENTAL,
        status=status,
        lean=lean,
        reason=label,
        members=[_side(state, "fundamentals", "fundamentals", label, lean.value)],
        coverage=coverage,
        detail=detail,
    )


def _revision_periods_disagree(research) -> bool:
    """True when the stored EPS-revision periods contain both an improving and
    a deteriorating direction (STABLE and REVIEW periods do not count)."""
    summary = research.analyst_research
    if summary is None:
        return False
    directions = {period.direction.value for period in summary.revision_trend}
    return {"IMPROVING", "DETERIORATING"} <= directions


def _market_opinion_family(
    state: "ResearchState", lines: dict[str, Any], coverage_settings: CoverageSettings
) -> tuple[FamilyAssessment, list[Conflict]]:
    research = state.stock_research
    if research is None or "analysts" not in lines:
        return _unassessed(Family.MARKET_OPINION, "no research computed for this security"), []
    analysts, revisions = lines["analysts"], lines["revisions"]
    consensus = research.analyst_consensus
    analyst_coverage = None if consensus is None else consensus.coverage

    members: list[tuple[EvidenceSide, FamilyLean, FamilyStatus]] = []
    notes: list[str] = []
    for line, field_name, coverage in (
        (analysts, "analyst_activity", analyst_coverage),
        (revisions, "analyst_activity", None),
    ):
        lean = _family_lean(line.lean)
        if lean is FamilyLean.NONE:
            notes.append(f"{line.label_display} unassessed ({line.label})")
            continue
        member_status = (
            FamilyStatus.PROVISIONAL
            if coverage is not None and coverage < coverage_settings.full_confidence_at
            else FamilyStatus.ESTABLISHED
        )
        if line.domain == "revisions" and _revision_periods_disagree(research):
            # The stance reads the nearest fiscal period, but the later
            # period(s) point the other way: a split reading is not an
            # established direction, so it cannot anchor a HARD conflict.
            member_status = FamilyStatus.PROVISIONAL
            notes.append("revision periods disagree")
        members.append((_side(state, field_name, line.domain, line.label, line.lean.value), lean, member_status))

    if not members:
        return _unassessed(Family.MARKET_OPINION, "; ".join(notes) or "no analyst evidence", coverage=analyst_coverage), []

    sides = [side for side, _, _ in members]
    statuses = [status for _, _, status in members]
    status = FamilyStatus.PROVISIONAL if (
        FamilyStatus.PROVISIONAL in statuses or len(members) < 2
    ) else FamilyStatus.ESTABLISHED
    definite = [lean for _, lean, _ in members if lean in _SIGN]
    conflicts: list[Conflict] = []
    if FamilyLean.POSITIVE in definite and FamilyLean.NEGATIVE in definite:
        lean = FamilyLean.CONFLICTED
        strength = (
            ConflictStrength.HARD
            if all(member_status is FamilyStatus.ESTABLISHED for member_status in statuses)
            else ConflictStrength.SOFT
        )
        conflicts.append(
            Conflict(
                kind=ConflictKind.INTRA_FAMILY,
                strength=strength,
                rule_id="X2-intra-family-market-opinion",
                sides=sides,
                explanation=(
                    f"Analyst consensus ({analysts.label}) and analyst revisions ({revisions.label}) "
                    "point in opposite directions."
                ),
            )
        )
    elif definite:
        lean = definite[0]
    else:
        lean = FamilyLean.NEUTRAL
    reason = "; ".join([f"{side.domain}: {side.label}" for side in sides] + notes)
    return FamilyAssessment(
        family=Family.MARKET_OPINION, status=status, lean=lean, reason=reason, members=sides,
        coverage=analyst_coverage,
    ), conflicts


def _price_behaviour_family(
    state: "ResearchState", lines: dict[str, Any], coverage_settings: CoverageSettings, stale_price_days: int
) -> FamilyAssessment:
    research = state.stock_research
    if research is None or "technical" not in lines:
        return _unassessed(Family.PRICE_BEHAVIOUR, "no research computed for this security")
    line = lines["technical"]
    summary = research.technical_summary
    lean = _family_lean(line.lean)
    coverage = None if summary is None else summary.coverage
    if lean is FamilyLean.NONE:
        return _unassessed(Family.PRICE_BEHAVIOUR, f"technicals unassessed ({line.label})", coverage=coverage)
    reasons = [line.label]
    status = FamilyStatus.ESTABLISHED
    if coverage is not None and coverage < coverage_settings.full_confidence_at:
        status = FamilyStatus.PROVISIONAL
        reasons.append(f"indicator coverage {coverage:.0%}")
    if summary is not None:
        # Freshness of the newest price bar, not `summary.as_of` (compute date).
        if summary.price_data_through is None:
            status = FamilyStatus.PROVISIONAL
            reasons.append("date of the newest price bar not recorded; refresh technicals")
        elif assess_freshness("price", summary.price_data_through, state.evaluation_date, stale_price_days):
            status = FamilyStatus.PROVISIONAL
            reasons.append(f"newest price bar {summary.price_data_through} is older than {stale_price_days} days")
    return FamilyAssessment(
        family=Family.PRICE_BEHAVIOUR, status=status, lean=lean, reason="; ".join(reasons),
        members=[_side(state, "technicals", "technicals", line.label, line.lean.value)], coverage=coverage,
    )


def _cross_family_conflicts(families: list[FamilyAssessment]) -> list[Conflict]:
    conflicts: list[Conflict] = []
    voting = [family for family in families if family.status is not FamilyStatus.UNASSESSED and family.lean in _SIGN]
    for index, first in enumerate(voting):
        for second in voting[index + 1:]:
            if _SIGN[first.lean] is _SIGN[second.lean]:
                continue
            both_established = first.status is FamilyStatus.ESTABLISHED and second.status is FamilyStatus.ESTABLISHED
            conflicts.append(
                Conflict(
                    kind=ConflictKind.CROSS_FAMILY,
                    strength=ConflictStrength.HARD if both_established else ConflictStrength.SOFT,
                    rule_id="X1-cross-family",
                    sides=first.members + second.members,
                    explanation=(
                        f"{FAMILY_LABELS[first.family]} ({first.status.value.lower()}) is "
                        f"{first.lean.value.lower()}; {FAMILY_LABELS[second.family]} "
                        f"({second.status.value.lower()}) is {second.lean.value.lower()}."
                    ),
                )
            )
    return conflicts


def _agreement(families: list[FamilyAssessment], conflicts: list[Conflict]) -> tuple[Agreement, Direction]:
    assessed = [family for family in families if family.status is not FamilyStatus.UNASSESSED]
    # A detected HARD disagreement is positive evidence of a contest, so it
    # is surfaced even when too few families are assessed to call agreement.
    if any(conflict.strength is ConflictStrength.HARD for conflict in conflicts):
        return Agreement.CONTESTED, Direction.NONE
    if len(assessed) < 2:
        return Agreement.INSUFFICIENT, Direction.NONE
    leans = [family.lean for family in assessed]
    signs = {_SIGN[lean] for lean in leans if lean in _SIGN}
    if len(signs) > 1 or FamilyLean.CONFLICTED in leans:
        return Agreement.MIXED, Direction.NONE
    if not signs:
        return Agreement.NEUTRAL, Direction.NONE
    direction = next(iter(signs))
    agreeing = [family for family in assessed if family.lean in _SIGN]
    if len(agreeing) < 2:
        return Agreement.MIXED, Direction.NONE  # a single directional family is not agreement
    convergent = (
        len(assessed) == len(FAMILY_ORDER)
        and len(agreeing) == len(assessed)
        and all(family.status is FamilyStatus.ESTABLISHED for family in assessed)
        and not conflicts
    )
    return (Agreement.CONVERGENT if convergent else Agreement.LEANING), direction


def _non_voting(
    state: "ResearchState", lines: dict[str, Any], direction: Direction
) -> tuple[list[EvidenceSide], list[EvidenceSide], list[Conflict]]:
    context: list[EvidenceSide] = []
    synthesis: list[EvidenceSide] = []
    conflicts: list[Conflict] = []
    wanted = (
        # Both regime lines are read from the alignment assessment, so they cite
        # the alignment field's status/provenance -- not `state.macro`, a
        # separate snapshot that can be refreshed independently.
        ("macro", "donatien_alignment", context, ConflictKind.REGIME_VS_SECURITY, "X3-regime-vs-security"),
        ("external_calibration", "donatien_alignment", context, ConflictKind.REGIME_VS_SECURITY, "X3-regime-vs-security"),
        ("ai_research", "ai_rating", synthesis, ConflictKind.SYNTHESIS_DIVERGENCE, "X4-synthesis-divergence"),
        ("fundamentals", "fundamentals", synthesis, ConflictKind.SYNTHESIS_DIVERGENCE, "X4-synthesis-divergence"),
    )
    for line_key, field_name, bucket, kind, rule_id in wanted:
        line = lines.get(line_key)
        if line is None or line.lean in (None, StanceLean.INSUFFICIENT_DATA):
            continue
        # The stance's "fundamentals" line is the composite score's label; it
        # is a synthesis here, never the Fundamental family.
        domain = "composite_score" if line_key == "fundamentals" else line_key
        side = _side(state, field_name, domain, line.label, line.lean.value)
        bucket.append(side)
        line_direction = {StanceLean.POSITIVE: Direction.POSITIVE, StanceLean.NEGATIVE: Direction.NEGATIVE}.get(line.lean)
        if direction is not Direction.NONE and line_direction is not None and line_direction is not direction:
            conflicts.append(
                Conflict(
                    kind=kind,
                    strength=ConflictStrength.CONTEXT,
                    rule_id=rule_id,
                    sides=[side],
                    explanation=(
                        f"{domain} ({line.label}) points {line_direction.value.lower()} while the "
                        f"independent evidence families lean {direction.value.lower()}."
                    ),
                )
            )
    return context, synthesis, conflicts


def assess_conviction(
    state: "ResearchState", *, coverage: CoverageSettings, stale_price_days: int
) -> ConvictionAssessment:
    """Pure and deterministic: the same state and thresholds always produce
    the identical assessment (fixed family order, fixed rule order)."""
    lines: dict[str, Any] = {}
    if state.stock_research is not None:
        alignment = (
            AlignmentAssessment.model_validate(state.donatien_alignment.value)
            if state.donatien_alignment.value
            else None
        )
        lines = {line.domain: line for line in build_research_stance(state.stock_research, alignment=alignment).lines}

    fundamental = _fundamental_family(state, coverage)
    market, intra_conflicts = _market_opinion_family(state, lines, coverage)
    price = _price_behaviour_family(state, lines, coverage, stale_price_days)
    families = [fundamental, market, price]

    conflicts = _cross_family_conflicts(families) + intra_conflicts
    agreement, direction = _agreement(families, conflicts)
    context, synthesis, context_conflicts = _non_voting(state, lines, direction)
    return ConvictionAssessment(
        ticker=state.ticker,
        evaluation_date=state.evaluation_date,
        direction=direction,
        agreement=agreement,
        families=families,
        conflicts=conflicts + context_conflicts,
        context=context,
        synthesis=synthesis,
    )
