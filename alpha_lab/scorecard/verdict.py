"""Security Screener Verdict: a tier-weighted Fit Score + red-flag Gates +
Verdict tier, modeled on the general TECHNIQUE used by third-party
scorecard reports (blend several evidence dimensions into one score by
lifecycle-stage weighting, then let categorical gates override a merely
numeric result) -- applied here entirely to AlphaLab's OWN already-computed
evidence (`alpha_lab.screener.service.LiveResearchRecord`), never to any
third-party site's own per-security output.

Architectural boundary, explicit and deliberate: this module reads no
external report of any kind. `alpha_lab.providers.donatien` (Donatien
External Calibration) remains exactly what its own module docstring says
-- "not ground truth and not a stock-rating engine" -- and nothing here
changes that; this module does not import `alpha_lab.providers.donatien`,
`alpha_lab.alignment`, or `alpha_lab.calibration`, and Donatien's own
market-wide regime read is not one of this module's inputs. The
"scorecard/fit/verdict" idea being borrowed is a general pattern (weighted
blend + gates + verdict), not any specific report's data.

Deliberately its own top-level package, following the same one-directional
import convention `alpha_lab.research_stance`/`alpha_lab.evidence_coverage`
already established: this module freely imports `alpha_lab.screener`
(`LiveResearchRecord`, plus its existing `_ai_is_attributable`/
`_ai_is_scoring_eligible` AI-eligibility gate, reused rather than
duplicated) and `alpha_lab.database.models.AIResearchAnalysis`, but nothing
in `alpha_lab.screener`/`.strategy`/`.backtest`/`.portfolio`/`.ratings`/
`.factors` imports this module back -- `LiveResearchRecord.overall_score`
is untouched by anything here.

`ai_research` is deliberately EXCLUDED from `SecurityScreenerVerdict`'s own
Fit Score: per the user's explicit design, `SecurityScreenerVerdict`
(the quantitative/fundamental scorecard) and `AIResearchResult`/
`AIResearchAnalysis` (the document-evidence AI read) are combined
separately, once, in `AIFinalRating` -- folding `ai_research`'s
`category_scores` entry into the Fit Score too would double-count the same
AI evidence twice in one final blend.

All numeric thresholds below (tier boundaries, gate thresholds, blend
weights) are deliberately round, documented V1 defaults, NOT derived from
any real forward-return calibration study -- the same "reasonable
defaults, explicitly flagged as uncalibrated" precedent already used
elsewhere in this codebase (e.g. `alpha_lab.ai.rule_based._confidence`'s
10-match full-confidence threshold). See ARCHITECTURE.md for the explicit
flag that these are starting points, not validated cutoffs.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from alpha_lab.database.models import AIResearchAnalysis
from alpha_lab.screener.service import (
    LiveResearchRecord,
    _ai_is_attributable,
    _ai_is_scoring_eligible,
)

SCREENER_VERDICT_METHODOLOGY_VERSION = "screener-verdict-v1"
AI_FINAL_RATING_METHODOLOGY_VERSION = "ai-final-rating-v1"


class SecurityTier(StrEnum):
    """Real, objective proxy for "lifecycle stage" -- market capitalization
    size -- rather than a subjective growth/value judgment call this
    codebase has no real data to support. Unknown market cap defaults to
    the most conservative (SPECULATIVE) tier rather than guessing a size."""

    CORE = "CORE"
    GROWTH = "GROWTH"
    SPECULATIVE = "SPECULATIVE"


# Round, documented USD market-cap boundaries -- not calibrated.
_CORE_MIN_MARKET_CAP = 10_000_000_000.0
_GROWTH_MIN_MARKET_CAP = 2_000_000_000.0


def classify_tier(market_cap: float | None) -> SecurityTier:
    if market_cap is None:
        return SecurityTier.SPECULATIVE
    if market_cap >= _CORE_MIN_MARKET_CAP:
        return SecurityTier.CORE
    if market_cap >= _GROWTH_MIN_MARKET_CAP:
        return SecurityTier.GROWTH
    return SecurityTier.SPECULATIVE


# The seven LiveResearchRecord.category_scores dimensions that feed the Fit
# Score -- every existing screener category except ai_research (see module
# docstring for why). Coincidentally also seven dimensions, echoing the
# shape of the third-party scorecard this technique was inspired by, but
# not engineered to match it field-for-field -- these are AlphaLab's own
# actually-computed categories, not a relabeling of someone else's.
FIT_SCORE_CATEGORIES: tuple[str, ...] = (
    "earnings_growth",
    "analyst_revisions",
    "business_quality",
    "valuation",
    "momentum",
    "financial_strength",
    "shareholder_return",
)

# Tier-fit weighting: CORE favors quality/financial-strength/shareholder
# return (conservative); SPECULATIVE favors growth/momentum/revisions
# (aggressive); GROWTH is the balanced middle. Each row sums to 1.0.
_TIER_WEIGHTS: dict[SecurityTier, dict[str, float]] = {
    SecurityTier.CORE: {
        "earnings_growth": 0.10, "analyst_revisions": 0.10, "business_quality": 0.20,
        "valuation": 0.15, "momentum": 0.10, "financial_strength": 0.20,
        "shareholder_return": 0.15,
    },
    SecurityTier.GROWTH: {
        "earnings_growth": 0.20, "analyst_revisions": 0.15, "business_quality": 0.15,
        "valuation": 0.10, "momentum": 0.15, "financial_strength": 0.15,
        "shareholder_return": 0.10,
    },
    SecurityTier.SPECULATIVE: {
        "earnings_growth": 0.25, "analyst_revisions": 0.20, "business_quality": 0.10,
        "valuation": 0.05, "momentum": 0.25, "financial_strength": 0.10,
        "shareholder_return": 0.05,
    },
}

# Fit Score color bands, matching the source technique's own green/amber/
# red convention -- a generic, non-proprietary three-band scheme.
FIT_SCORE_STRONG_THRESHOLD = 70.0
FIT_SCORE_WEAK_THRESHOLD = 40.0

# A ticker with only one or two of the seven categories available (e.g. a
# commodity/FX reference series with no fundamentals at all) must never
# get a misleadingly precise-looking Fit Score from that thin a base --
# fewer than this many available categories forces `fit_score=None`
# (INSUFFICIENT_DATA), the same "not enough evidence to judge" treatment
# `CATEGORY_MINIMUM_METRICS` already applies to each individual category.
_MIN_FIT_SCORE_CATEGORIES = 4

# Hard gate: absolute (not relative-to-universe) balance-sheet distress
# signals -- real raw metrics already computed by
# `alpha_lab.ratings.calculate_quality_factors`, not percentile scores
# (a percentile is only meaningful relative to AlphaLab's own small
# universe; solvency risk is not).
_HARD_GATE_MAX_DEBT_EBITDA = 6.0
_HARD_GATE_MIN_INTEREST_COVERAGE = 1.0

# Caution gates: relative-to-universe percentile weakness on a single
# dimension severe enough to cap the verdict even if the blended Fit Score
# would otherwise pass.
_CAUTION_GATE_VALUATION_PERCENTILE = 20.0
_CAUTION_GATE_REVISIONS_PERCENTILE = 20.0
_CAUTION_GATE_FINANCIAL_STRENGTH_PERCENTILE = 25.0

# AIFinalRating: how much weight ai_rating gets once genuinely available
# (attributable AND scoring-eligible) alongside a real Fit Score.
_AI_BLEND_WEIGHT = 0.3
_FIT_BLEND_WEIGHT = 1.0 - _AI_BLEND_WEIGHT

# AIResearchResult.risk_score (0 to +2, higher = more risk) at or above
# this is treated as its own caution-equivalent gate on the final verdict.
_AI_RISK_GATE_THRESHOLD = 1.5


class ScreenerVerdict(StrEnum):
    STRONG_FIT = "STRONG_FIT"
    INTEREST = "INTEREST"
    NO_INTEREST = "NO_INTEREST"
    # Fewer than half of FIT_SCORE_CATEGORIES had a computable value --
    # never forced to a numeric verdict from too little evidence.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SecurityScreenerVerdict(BaseModel):
    ticker: str
    tier: SecurityTier
    fit_score: float | None = Field(default=None, ge=0, le=100)
    # Only the categories that actually had a value -- absent categories
    # are omitted, never fabricated as 0 or 50.
    dimension_scores: dict[str, float]
    dimension_coverage: float = Field(ge=0, le=1)
    hard_gates: list[str] = Field(default_factory=list)
    caution_gates: list[str] = Field(default_factory=list)
    verdict: ScreenerVerdict
    methodology_version: str = SCREENER_VERDICT_METHODOLOGY_VERSION


class AIFinalRating(BaseModel):
    ticker: str
    screener_verdict: SecurityScreenerVerdict
    ai_rating: float | None = Field(default=None, ge=0, le=100)
    ai_provider: str | None = None
    ai_risk_score: float | None = None
    final_rating: float | None = Field(default=None, ge=0, le=100)
    final_verdict: ScreenerVerdict
    gates_triggered: list[str] = Field(default_factory=list)
    methodology_version: str = AI_FINAL_RATING_METHODOLOGY_VERSION


def _evaluate_hard_gates(record: LiveResearchRecord) -> list[str]:
    """Absolute red flags -- a hard gate always forces NO_INTEREST
    regardless of how high the blended Fit Score would otherwise be,
    mirroring the source technique's own "capped by red-flag gates"
    convention."""
    gates: list[str] = []
    if record.ethical_status != "PASS":
        gates.append("ethical_exclusion")
    debt_ebitda = record.raw_metrics.get("debt_ebitda")
    if debt_ebitda is not None and debt_ebitda > _HARD_GATE_MAX_DEBT_EBITDA:
        gates.append("severe_leverage")
    interest_coverage = record.raw_metrics.get("interest_coverage")
    if interest_coverage is not None and interest_coverage < _HARD_GATE_MIN_INTEREST_COVERAGE:
        gates.append("interest_coverage_shortfall")
    return gates


def _evaluate_caution_gates(record: LiveResearchRecord) -> list[str]:
    """Relative-to-universe weakness on one dimension severe enough to cap
    the verdict at INTEREST even when the blended Fit Score alone would
    reach STRONG_FIT."""
    gates: list[str] = []
    valuation = record.category_scores.get("valuation")
    if valuation is not None and valuation < _CAUTION_GATE_VALUATION_PERCENTILE:
        gates.append("valuation_extreme")
    revisions = record.category_scores.get("analyst_revisions")
    if revisions is not None and revisions < _CAUTION_GATE_REVISIONS_PERCENTILE:
        gates.append("negative_revisions")
    strength = record.category_scores.get("financial_strength")
    if strength is not None and strength < _CAUTION_GATE_FINANCIAL_STRENGTH_PERCENTILE:
        gates.append("weak_balance_sheet_relative")
    return gates


def _score_verdict(
    score: float | None, *, hard_gates: list[str], caution_gates: list[str]
) -> ScreenerVerdict:
    if hard_gates:
        return ScreenerVerdict.NO_INTEREST
    if score is None:
        return ScreenerVerdict.INSUFFICIENT_DATA
    if score < FIT_SCORE_WEAK_THRESHOLD:
        return ScreenerVerdict.NO_INTEREST
    if score >= FIT_SCORE_STRONG_THRESHOLD and not caution_gates:
        return ScreenerVerdict.STRONG_FIT
    return ScreenerVerdict.INTEREST


def build_security_screener_verdict(record: LiveResearchRecord) -> SecurityScreenerVerdict:
    """Pure construction from an already-computed `LiveResearchRecord` --
    no provider call, no database read, no new upstream scoring. Reuses
    `record.category_scores`/`record.raw_metrics`/`record.market_cap`/
    `record.ethical_status` exactly as already computed by
    `MarketScreenerService`; computes no new category score of its own."""
    tier = classify_tier(record.market_cap)
    weights = _TIER_WEIGHTS[tier]
    available = {
        name: record.category_scores[name]
        for name in FIT_SCORE_CATEGORIES
        if record.category_scores.get(name) is not None
    }
    available_weight = sum(weights[name] for name in available)
    fit_score = (
        round(
            sum(value * weights[name] for name, value in available.items()) / available_weight,
            2,
        )
        if available_weight and len(available) >= _MIN_FIT_SCORE_CATEGORIES
        else None
    )
    hard_gates = _evaluate_hard_gates(record)
    caution_gates = _evaluate_caution_gates(record)
    return SecurityScreenerVerdict(
        ticker=record.ticker,
        tier=tier,
        fit_score=fit_score,
        dimension_scores=available,
        dimension_coverage=round(len(available) / len(FIT_SCORE_CATEGORIES), 4),
        hard_gates=hard_gates,
        caution_gates=caution_gates,
        verdict=_score_verdict(fit_score, hard_gates=hard_gates, caution_gates=caution_gates),
    )


def build_ai_final_rating(
    verdict: SecurityScreenerVerdict, ai: AIResearchAnalysis | None
) -> AIFinalRating:
    """Blends `verdict.fit_score` with a genuinely usable
    `AIResearchAnalysis.ai_rating` -- reusing
    `alpha_lab.screener.service`'s own `_ai_is_attributable`/
    `_ai_is_scoring_eligible` gate rather than re-deriving it, so an AI
    analysis that isn't trusted to move `LiveResearchRecord.overall_score`
    is equally never trusted to move this rating. A missing or untrusted
    AI analysis is treated as ABSENT (excluded from the blend), never as a
    zero or neutral score -- `final_rating` falls back to `fit_score`
    alone in that case, the same "missing stays missing" convention every
    other weighted average in this codebase already follows."""
    ai_usable = ai is not None and _ai_is_attributable(ai) and _ai_is_scoring_eligible(ai)
    ai_rating = ai.ai_rating if ai_usable else None
    ai_provider = ai.provider if ai_usable else None
    ai_risk_score = ai.component_scores.get("risk_score") if ai_usable else None

    if verdict.fit_score is not None and ai_rating is not None:
        final_rating = round(
            verdict.fit_score * _FIT_BLEND_WEIGHT + ai_rating * _AI_BLEND_WEIGHT, 2
        )
    elif verdict.fit_score is not None:
        final_rating = verdict.fit_score
    else:
        final_rating = ai_rating

    ai_risk_gated = ai_risk_score is not None and ai_risk_score >= _AI_RISK_GATE_THRESHOLD
    gates_triggered = list(verdict.hard_gates) + list(verdict.caution_gates)
    if ai_risk_gated:
        gates_triggered.append("elevated_ai_risk_signal")

    if verdict.hard_gates:
        final_verdict = ScreenerVerdict.NO_INTEREST
    else:
        final_verdict = _score_verdict(
            final_rating,
            hard_gates=[],
            caution_gates=verdict.caution_gates + (["elevated_ai_risk_signal"] if ai_risk_gated else []),
        )

    return AIFinalRating(
        ticker=verdict.ticker,
        screener_verdict=verdict,
        ai_rating=ai_rating,
        ai_provider=ai_provider,
        ai_risk_score=ai_risk_score,
        final_rating=final_rating,
        final_verdict=final_verdict,
        gates_triggered=gates_triggered,
    )
