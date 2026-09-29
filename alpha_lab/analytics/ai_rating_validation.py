"""AI Rating empirical validation protocol (roadmap Phase 7).

Measurement only. Nothing here is wired into `HistoricalScoringService`,
`alpha_lab.backtest`, `AIResearchAssessment`'s own score/rating math, or
any ranking path -- a validation result is evidence that a relationship
existed historically, not a licence for the signal to influence decisions.
Builds on (and leaves untouched) `alpha_lab.analytics.signal_predictive_value`,
reusing its `_correlate`/`_price_series`/`_forward_return_from` so the
forward-return anchoring (first trading day strictly after the snapshot)
has exactly one implementation.

**The protocol is frozen.** `AI_RATING_VALIDATION_PROTOCOL_V1` is a frozen
dataclass fixed before any production validation result was evaluated. A
threshold change (a different horizon, a lower ticker minimum, dropping
Pearson, ...) is a new protocol *version*, never an edit of this one --
a test pins V1's exact values so an accidental edit fails loudly. Reports
carry `protocol_version`, so a verdict is always attributable to the
protocol that produced it and a later protocol never overwrites it.

**Observations are grouped by AI methodology and never pooled.** An
`AIMethodologyKey` is (methodology_version, provider, model, prompt_version)
as recorded on each persisted `AIResearchAssessment`; one report is
produced per key, so "AI Rating v3 / deterministic provider has N valid
observations" is stated about exactly that methodology.

**Independence is horizon-specific.** For a `h`-trading-day forward return,
two observations of the same ticker are only both kept if their return
windows do not overlap (entry positions at least `h` trading days apart).
The 30-observation floor is evaluated per horizon on that horizon's own
non-overlapping set -- the same snapshots are *not* assumed independent for
all three horizons, and a 60-day set is normally far smaller than a 5-day
one. Known residual limitation, stated rather than hidden: observations of
*different* tickers over the same calendar period share market-wide
moves, so even non-overlapping same-ticker windows are not fully
independent cross-sectionally. The criteria below are therefore necessary
evidence, not proof.

**Statistics appear only for testable horizons.** Below the sample floors a
horizon reports its observation/ticker counts and nothing else -- a
correlation computed from a handful of points is exactly the
impressive-looking number this protocol exists to prevent.

Criteria (criterion-level results are reported; there is deliberately no
combined score):

- A `min_history`: at least `min_supporting_horizons` horizons meet the
  per-horizon floors (>= 30 independent observations, >= 8 distinct
  tickers).
- B `association_consistency`: Pearson and Spearman agree in direction in
  every testable horizon.
- C `horizon_stability`: the association is *supported* (both correlations
  positive and Spearman >= 2/sqrt(n)) in at least
  `min_supporting_horizons` testable horizons.
- D `incremental_information`: `forward_return ~ deterministic_score +
  ai_score` (both z-scored, same observations, plain OLS): the AI term is
  positive with t >= `incremental_min_t_stat` in at least
  `min_supporting_horizons` testable horizons. This is an *incremental
  association analysis*, not a formal partial-correlation test.
- E `confidence_calibration` (informational, does not gate the status):
  split at a fixed `high_confidence_threshold`; each bucket needs
  `min_confidence_bucket_observations`; consistent with confidence being
  informative only if the high bucket is positive, stronger than the low
  bucket by at least `min_confidence_spearman_difference`, *and* that gap
  is significant under a Fisher-z comparison of the two correlations
  (`min_confidence_difference_z`) -- the fixed gap alone passes by chance.

Status: NOT_TESTABLE (no scored assessments for the methodology),
INSUFFICIENT_HISTORY (no horizon testable), TESTABLE_NO_VALIDATION (fewer
than `min_supporting_horizons` horizons testable, so stability cannot be
judged), VALIDATION_SUPPORTED (A-D all pass), VALIDATION_NOT_SUPPORTED
(A passes, and B, C or D does not).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
import math

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from alpha_lab.analytics.signal_predictive_value import (
    SignalObservation,
    _correlate,
    _forward_return_from,
    _price_series,
)
from alpha_lab.config import Settings
from alpha_lab.research.service import ResearchService


@dataclass(frozen=True)
class AIRatingValidationProtocol:
    version: str
    horizons: tuple[int, ...]
    min_independent_observations: int
    min_distinct_tickers: int
    min_supporting_horizons: int
    incremental_min_t_stat: float
    high_confidence_threshold: float
    min_confidence_bucket_observations: int
    min_confidence_spearman_difference: float
    min_confidence_difference_z: float


AI_RATING_VALIDATION_PROTOCOL_V1 = AIRatingValidationProtocol(
    version="ai-rating-validation-v1",
    horizons=(5, 20, 60),
    min_independent_observations=30,
    min_distinct_tickers=8,
    min_supporting_horizons=2,
    incremental_min_t_stat=2.0,
    high_confidence_threshold=0.75,
    min_confidence_bucket_observations=15,
    min_confidence_spearman_difference=0.10,
    min_confidence_difference_z=1.645,
)


class ValidationStatus(StrEnum):
    NOT_TESTABLE = "NOT_TESTABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TESTABLE_NO_VALIDATION = "TESTABLE_NO_VALIDATION"
    VALIDATION_SUPPORTED = "VALIDATION_SUPPORTED"
    VALIDATION_NOT_SUPPORTED = "VALIDATION_NOT_SUPPORTED"


class CriterionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTABLE = "NOT_TESTABLE"


@dataclass(frozen=True)
class AIMethodologyKey:
    methodology_version: str
    provider: str
    model: str
    prompt_version: str


@dataclass
class _Candidate:
    ticker: str
    created_at: datetime
    as_of: date
    snapshot_id: str
    ai_score: float
    deterministic_score: float | None
    confidence: float
    research_refresh_version_id: str | None


@dataclass
class ValidationObservation:
    ticker: str
    as_of: date
    snapshot_id: str
    ai_score: float
    deterministic_score: float | None
    confidence: float
    research_refresh_version_id: str | None
    forward_return: float


@dataclass
class IncrementalAssociation:
    """Standardized OLS `forward_return ~ deterministic_score + ai_score`.
    `ai_beta` is the change in forward return per 1 SD of AI score holding
    the deterministic score constant; the t-stat assumes independent
    residuals (see module docstring's residual-dependence caveat)."""

    n: int
    ai_beta: float
    ai_t_stat: float
    deterministic_beta: float
    deterministic_t_stat: float


@dataclass
class ConfidenceSplit:
    high_n: int
    low_n: int
    high_spearman: float | None
    low_spearman: float | None
    result: CriterionResult


@dataclass
class HorizonResult:
    horizon_days: int
    independent_observations: int
    distinct_tickers: int
    testable: bool
    pearson: float | None = None
    spearman: float | None = None
    direction_agrees: bool | None = None
    association_supported: bool | None = None
    deterministic_spearman: float | None = None
    incremental: IncrementalAssociation | None = None
    incremental_result: CriterionResult = CriterionResult.NOT_TESTABLE
    confidence_split: ConfidenceSplit | None = None


@dataclass
class CriterionOutcome:
    name: str
    result: CriterionResult
    detail: str


@dataclass
class AIRatingValidationReport:
    protocol_version: str
    methodology: AIMethodologyKey
    scored_snapshots: int
    review_excluded: int
    horizons: list[HorizonResult]
    criteria: list[CriterionOutcome]
    status: ValidationStatus
    observations: dict[int, list[ValidationObservation]] = field(repr=False, default_factory=dict)


def select_independent_observations(
    candidates: list[_Candidate], prices_by_ticker: dict[str, pd.Series], horizon: int
) -> list[ValidationObservation]:
    """Per ticker, chronologically keep an observation only if its entry
    trading-day position is at least `horizon` positions after the last
    kept one -- i.e. its forward-return window does not overlap the last
    kept window. Deterministic (earliest of a cluster wins)."""
    selected: list[ValidationObservation] = []
    by_ticker: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        by_ticker.setdefault(candidate.ticker, []).append(candidate)
    for ticker in sorted(by_ticker):
        prices = prices_by_ticker.get(ticker)
        if prices is None or prices.empty:
            continue
        last_position: int | None = None
        for candidate in sorted(by_ticker[ticker], key=lambda c: (c.created_at, c.snapshot_id)):
            forward_return = _forward_return_from(prices, candidate.as_of, horizon)
            if forward_return is None:
                continue
            position = int(prices.index.searchsorted(pd.Timestamp(candidate.as_of), side="right"))
            if last_position is not None and position < last_position + horizon:
                continue
            last_position = position
            selected.append(
                ValidationObservation(
                    ticker=ticker, as_of=candidate.as_of, snapshot_id=candidate.snapshot_id,
                    ai_score=candidate.ai_score, deterministic_score=candidate.deterministic_score,
                    confidence=candidate.confidence,
                    research_refresh_version_id=candidate.research_refresh_version_id,
                    forward_return=forward_return,
                )
            )
    return selected


def _zscore(values: np.ndarray) -> np.ndarray | None:
    std = float(values.std())
    if std == 0.0 or math.isnan(std):
        return None
    return (values - values.mean()) / std


def incremental_association(
    deterministic: list[float], ai: list[float], forward_returns: list[float]
) -> IncrementalAssociation | None:
    """`None` when not computable (too few points, a constant predictor, a
    singular design, or a perfect fit leaving no residual variance)."""
    n = len(forward_returns)
    if n < 4:
        return None
    z_det = _zscore(np.asarray(deterministic, dtype=float))
    z_ai = _zscore(np.asarray(ai, dtype=float))
    if z_det is None or z_ai is None:
        return None
    y = np.asarray(forward_returns, dtype=float)
    design = np.column_stack([np.ones(n), z_det, z_ai])
    try:
        xtx_inverse = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None
    beta = xtx_inverse @ design.T @ y
    residuals = y - design @ beta
    sigma_squared = float(residuals @ residuals) / (n - 3)
    standard_errors = np.sqrt(np.diag(sigma_squared * xtx_inverse))
    if standard_errors[1] == 0.0 or standard_errors[2] == 0.0 or not np.all(np.isfinite(standard_errors)):
        return None
    return IncrementalAssociation(
        n=n,
        ai_beta=float(beta[2]),
        ai_t_stat=float(beta[2] / standard_errors[2]),
        deterministic_beta=float(beta[1]),
        deterministic_t_stat=float(beta[1] / standard_errors[1]),
    )


def _spearman(observations: list[ValidationObservation], value, horizon: int) -> float | None:
    return _correlate(
        [SignalObservation(o.ticker, o.as_of, value(o), o.forward_return) for o in observations],
        forward_days=horizon,
        signal_name="ai_rating_validation",
    ).spearman


def _confidence_split(
    observations: list[ValidationObservation], horizon: int, protocol: AIRatingValidationProtocol
) -> ConfidenceSplit:
    high = [o for o in observations if o.confidence >= protocol.high_confidence_threshold]
    low = [o for o in observations if o.confidence < protocol.high_confidence_threshold]
    floor = protocol.min_confidence_bucket_observations
    if len(high) < floor or len(low) < floor:
        return ConfidenceSplit(len(high), len(low), None, None, CriterionResult.NOT_TESTABLE)
    high_spearman = _spearman(high, lambda o: o.ai_score, horizon)
    low_spearman = _spearman(low, lambda o: o.ai_score, horizon)
    if high_spearman is None or low_spearman is None:
        return ConfidenceSplit(len(high), len(low), high_spearman, low_spearman, CriterionResult.NOT_TESTABLE)
    # A fixed effect-size gap alone passes by chance at moderate bucket
    # sizes, so the high bucket must also be stronger by a Fisher-z test of
    # two independent correlations (clipped away from +/-1). Clustering
    # understates the standard error; E is informational for that reason.
    def _fisher(r: float) -> float:
        return math.atanh(max(-0.999, min(0.999, r)))

    z_difference = (_fisher(high_spearman) - _fisher(low_spearman)) / math.sqrt(
        1 / (len(high) - 3) + 1 / (len(low) - 3)
    )
    consistent = (
        high_spearman > 0
        and high_spearman - low_spearman >= protocol.min_confidence_spearman_difference
        and z_difference >= protocol.min_confidence_difference_z
    )
    return ConfidenceSplit(
        len(high), len(low), high_spearman, low_spearman,
        CriterionResult.PASS if consistent else CriterionResult.FAIL,
    )


def analyze_horizon(
    observations: list[ValidationObservation], horizon: int, protocol: AIRatingValidationProtocol
) -> HorizonResult:
    n = len(observations)
    tickers = len({o.ticker for o in observations})
    testable = n >= protocol.min_independent_observations and tickers >= protocol.min_distinct_tickers
    result = HorizonResult(horizon_days=horizon, independent_observations=n, distinct_tickers=tickers, testable=testable)
    if not testable:
        return result

    correlation = _correlate(
        [SignalObservation(o.ticker, o.as_of, o.ai_score, o.forward_return) for o in observations],
        forward_days=horizon,
        signal_name="ai_rating.score",
    )
    result.pearson, result.spearman = correlation.pearson, correlation.spearman
    if correlation.pearson is not None and correlation.spearman is not None:
        result.direction_agrees = (correlation.pearson > 0) == (correlation.spearman > 0)
        result.association_supported = (
            correlation.pearson > 0
            and correlation.spearman > 0
            and correlation.spearman >= 2 / math.sqrt(n)
        )
    else:
        result.direction_agrees = False
        result.association_supported = False

    both = [o for o in observations if o.deterministic_score is not None]
    if len(both) >= protocol.min_independent_observations:
        result.deterministic_spearman = _spearman(both, lambda o: o.deterministic_score, horizon)
        incremental = incremental_association(
            [o.deterministic_score for o in both], [o.ai_score for o in both], [o.forward_return for o in both]
        )
        result.incremental = incremental
        if incremental is None:
            result.incremental_result = CriterionResult.FAIL
        elif incremental.ai_beta > 0 and incremental.ai_t_stat >= protocol.incremental_min_t_stat:
            result.incremental_result = CriterionResult.PASS
        else:
            result.incremental_result = CriterionResult.FAIL

    result.confidence_split = _confidence_split(observations, horizon, protocol)
    return result


def _count_criterion(name: str, passing: int, testable: int, protocol: AIRatingValidationProtocol, what: str) -> CriterionOutcome:
    needed = protocol.min_supporting_horizons
    if testable < needed:
        return CriterionOutcome(name, CriterionResult.NOT_TESTABLE, f"{testable} testable horizon(s); need {needed}")
    result = CriterionResult.PASS if passing >= needed else CriterionResult.FAIL
    return CriterionOutcome(name, result, f"{what} in {passing}/{testable} testable horizon(s); need {needed}")


def build_validation_report(
    methodology: AIMethodologyKey,
    candidates: list[_Candidate],
    prices_by_ticker: dict[str, pd.Series],
    *,
    review_excluded: int = 0,
    protocol: AIRatingValidationProtocol = AI_RATING_VALIDATION_PROTOCOL_V1,
) -> AIRatingValidationReport:
    observations: dict[int, list[ValidationObservation]] = {}
    horizons: list[HorizonResult] = []
    for horizon in protocol.horizons:
        observations[horizon] = select_independent_observations(candidates, prices_by_ticker, horizon)
        horizons.append(analyze_horizon(observations[horizon], horizon, protocol))

    testable = [h for h in horizons if h.testable]
    criteria: list[CriterionOutcome] = []

    a_pass = len(testable) >= protocol.min_supporting_horizons
    criteria.append(
        CriterionOutcome(
            "A_min_history",
            CriterionResult.PASS if a_pass else CriterionResult.NOT_TESTABLE,
            f"{len(testable)}/{len(horizons)} horizon(s) meet >={protocol.min_independent_observations} "
            f"independent observations across >={protocol.min_distinct_tickers} tickers; need {protocol.min_supporting_horizons}",
        )
    )
    if testable:
        disagreeing = [h.horizon_days for h in testable if not h.direction_agrees]
        criteria.append(
            CriterionOutcome(
                "B_association_consistency",
                CriterionResult.FAIL if disagreeing else CriterionResult.PASS,
                f"Pearson/Spearman disagree in horizon(s) {disagreeing}" if disagreeing
                else "Pearson and Spearman agree in every testable horizon",
            )
        )
    else:
        criteria.append(CriterionOutcome("B_association_consistency", CriterionResult.NOT_TESTABLE, "no testable horizon"))
    criteria.append(
        _count_criterion(
            "C_horizon_stability", sum(1 for h in testable if h.association_supported), len(testable),
            protocol, "association supported",
        )
    )
    criteria.append(
        _count_criterion(
            "D_incremental_information",
            sum(1 for h in testable if h.incremental_result == CriterionResult.PASS),
            sum(1 for h in testable if h.incremental_result != CriterionResult.NOT_TESTABLE),
            protocol, "incremental AI term positive with sufficient t-stat",
        )
    )
    splits = [h.confidence_split for h in testable if h.confidence_split is not None and h.confidence_split.result != CriterionResult.NOT_TESTABLE]
    if not splits:
        criteria.append(CriterionOutcome("E_confidence_calibration", CriterionResult.NOT_TESTABLE, "no horizon with both confidence buckets populated (informational)"))
    else:
        all_pass = all(s.result == CriterionResult.PASS for s in splits)
        criteria.append(
            CriterionOutcome(
                "E_confidence_calibration",
                CriterionResult.PASS if all_pass else CriterionResult.FAIL,
                f"high-confidence bucket stronger in {sum(1 for s in splits if s.result == CriterionResult.PASS)}/{len(splits)} testable split(s) (informational)",
            )
        )

    if not candidates:
        status = ValidationStatus.NOT_TESTABLE
    elif not testable:
        status = ValidationStatus.INSUFFICIENT_HISTORY
    elif not a_pass:
        status = ValidationStatus.TESTABLE_NO_VALIDATION
    else:
        gating = [c for c in criteria if c.name in ("B_association_consistency", "C_horizon_stability", "D_incremental_information")]
        status = (
            ValidationStatus.VALIDATION_SUPPORTED
            if all(c.result == CriterionResult.PASS for c in gating)
            else ValidationStatus.VALIDATION_NOT_SUPPORTED
        )
    return AIRatingValidationReport(
        protocol_version=protocol.version, methodology=methodology, scored_snapshots=len(candidates),
        review_excluded=review_excluded, horizons=horizons, criteria=criteria, status=status,
        observations=observations,
    )


def load_candidates(
    engine: Engine, settings: Settings, tickers: list[str]
) -> tuple[dict[AIMethodologyKey, list[_Candidate]], dict[AIMethodologyKey, int], dict[str, pd.Series]]:
    """Every persisted `ResearchSnapshot` carrying an `AIResearchAssessment`,
    grouped by methodology. A snapshot's real persistence time
    (`created_at`) is its `as_of` -- never its `evaluation_date` (same
    rationale as `signal_predictive_value._collect_snapshot_domain_observations`).
    An assessment with `score is None` (REVIEW) is counted in
    `review_excluded`, never given an invented score."""
    service = ResearchService(engine, settings)
    candidates: dict[AIMethodologyKey, list[_Candidate]] = {}
    review_excluded: dict[AIMethodologyKey, int] = {}
    prices: dict[str, pd.Series] = {}
    for raw_ticker in tickers:
        ticker = raw_ticker.strip().upper()
        prices[ticker] = _price_series(engine, ticker)
        for entry in service.get_research_history(ticker):
            research = service.get_research_snapshot(entry.snapshot_id)
            assessment = None if research is None else research.ai_research_assessment
            if assessment is None:
                continue
            key = AIMethodologyKey(
                assessment.methodology_version, assessment.source, assessment.model, assessment.prompt_version
            )
            candidates.setdefault(key, [])
            review_excluded.setdefault(key, 0)
            if assessment.score is None:
                review_excluded[key] += 1
                continue
            candidates[key].append(
                _Candidate(
                    ticker=ticker, created_at=entry.created_at, as_of=entry.created_at.date(),
                    snapshot_id=entry.snapshot_id, ai_score=assessment.score,
                    deterministic_score=research.overall_score, confidence=assessment.confidence,
                    research_refresh_version_id=assessment.research_refresh_version_id,
                )
            )
    return candidates, review_excluded, prices


def validate_ai_rating(
    engine: Engine,
    settings: Settings,
    tickers: list[str],
    *,
    protocol: AIRatingValidationProtocol = AI_RATING_VALIDATION_PROTOCOL_V1,
) -> list[AIRatingValidationReport]:
    """One report per AI methodology found in the persisted snapshot
    history (never pooled). An empty list means no `ResearchSnapshot` in
    the requested tickers carries an AI Research Rating at all."""
    candidates, review_excluded, prices = load_candidates(engine, settings, tickers)
    return [
        build_validation_report(key, candidates[key], prices, review_excluded=review_excluded[key], protocol=protocol)
        for key in sorted(candidates, key=lambda k: (k.methodology_version, k.provider, k.model, k.prompt_version))
    ]
