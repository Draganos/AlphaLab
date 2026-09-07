"""Phase 2B: deterministic, categorical-only alignment between AlphaLab
Macro Regime (`alpha_lab.macro`) and Donatien External Calibration
(`alpha_lab.providers.donatien` / `alpha_lab.calibration`).

Scope (approved before implementation): compares two ALREADY-COMPUTED
evidence layers and produces one of four categorical labels. There is no
numeric score anywhere in this module -- no `alignment_score`,
`conviction_score`, weighted average, or hidden percentage. See
`Alignment` for the closed vocabulary. This module is pure computation
(no I/O, no network, no database) -- see `alpha_lab.alignment.service`
for persistence.

Neither Market Regime nor Donatien is ground truth for the other; this
module only states whether the two agree, disagree, or cannot be
compared, and always retains the raw categorical inputs that produced
that verdict on `AlignmentAssessment` for auditability -- "why was this
date ALIGNED?" is always answerable by reading the stored fields, never
by re-deriving an opaque calculation.

Donatien mapping (the one genuinely structured field it exposes):
`dominant_regime` is free-text prose with no confirmed vocabulary,
`confidence` is a word not a number, and `defensiveness` has no
documented scale (see `alpha_lab.providers.donatien`'s module docstring)
-- none of the three is used to derive the categorical lean below; they
are carried through on `AlignmentAssessment` only as audit-only context,
never as classifier input. `scenario_weights` is the one structured field
(a plain scenario-name -> weight mapping), and its four consistently
observed scenario names are a standard institutional growth/inflation
quadrant framework, not an AlphaLab invention:
  - "Reacceleration" (rising growth) and "Soft Landing" (moderating,
    non-recessionary growth) are the two growth-supportive quadrants ->
    classified CONSTRUCTIVE.
  - "Stagflation" (rising inflation, weakening growth) and "Deflationary
    Bust" (contracting growth and prices) are the two growth-adverse
    quadrants -> classified DEFENSIVE.
Donatien's lean is whichever bucket carries the greater summed weight; a
tie, or a payload whose `scenario_weights` contains none of these four
recognized names (a taxonomy AlphaLab has not observed), is UNKNOWN --
never guessed.

Market Regime's own `MacroRegime.REVIEW` (neither VIX nor the yield curve
spread was available) always maps to `Alignment.INSUFFICIENT_DATA`: there
is no textbook basis for a deterministic rule that would let an
unassessable market regime still produce ALIGNED/CONFLICT/NEUTRAL.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from alpha_lab.macro.regime import MacroAssessment, MacroRegime
from alpha_lab.providers.donatien import DonatienCalibration, source_observed_at

ALIGNMENT_METHODOLOGY_VERSION = "alignment-v1"

# Standard growth/inflation quadrant scenario names, as consistently
# observed in the Donatien payload's `scenario_weights` (see module
# docstring). Any other key is preserved verbatim in the audit record but
# does not participate in the CONSTRUCTIVE/DEFENSIVE classification.
CONSTRUCTIVE_SCENARIOS: frozenset[str] = frozenset({"Reacceleration", "Soft Landing"})
DEFENSIVE_SCENARIOS: frozenset[str] = frozenset({"Stagflation", "Deflationary Bust"})


class DonatienLean(StrEnum):
    CONSTRUCTIVE = "CONSTRUCTIVE"
    DEFENSIVE = "DEFENSIVE"
    MIXED = "MIXED"
    # scenario_weights present but none of the recognized scenario names
    # appear in it -- a taxonomy change AlphaLab has not verified.
    UNKNOWN = "UNKNOWN"


class Alignment(StrEnum):
    ALIGNED = "ALIGNED"
    CONFLICT = "CONFLICT"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def classify_donatien_lean(scenario_weights: dict[str, float]) -> DonatienLean:
    """Pure, deterministic classification of Donatien's one structured
    directional field. Never guesses: a tie or an unrecognized taxonomy
    both resolve to a non-directional label rather than an invented one."""
    constructive_weight = sum(
        weight for name, weight in scenario_weights.items() if name in CONSTRUCTIVE_SCENARIOS
    )
    defensive_weight = sum(
        weight for name, weight in scenario_weights.items() if name in DEFENSIVE_SCENARIOS
    )
    if constructive_weight == 0 and defensive_weight == 0:
        return DonatienLean.UNKNOWN
    if constructive_weight > defensive_weight:
        return DonatienLean.CONSTRUCTIVE
    if defensive_weight > constructive_weight:
        return DonatienLean.DEFENSIVE
    return DonatienLean.MIXED


# Market regime x Donatien lean -> Alignment. MacroRegime.REVIEW and
# DonatienLean.UNKNOWN are resolved to INSUFFICIENT_DATA before this table
# is consulted -- see build_alignment_assessment.
_ALIGNMENT_TABLE: dict[tuple[MacroRegime, DonatienLean], Alignment] = {
    (MacroRegime.RISK_ON, DonatienLean.CONSTRUCTIVE): Alignment.ALIGNED,
    (MacroRegime.RISK_ON, DonatienLean.DEFENSIVE): Alignment.CONFLICT,
    (MacroRegime.RISK_ON, DonatienLean.MIXED): Alignment.NEUTRAL,
    (MacroRegime.RISK_OFF, DonatienLean.DEFENSIVE): Alignment.ALIGNED,
    (MacroRegime.RISK_OFF, DonatienLean.CONSTRUCTIVE): Alignment.CONFLICT,
    (MacroRegime.RISK_OFF, DonatienLean.MIXED): Alignment.NEUTRAL,
    (MacroRegime.NEUTRAL, DonatienLean.CONSTRUCTIVE): Alignment.NEUTRAL,
    (MacroRegime.NEUTRAL, DonatienLean.DEFENSIVE): Alignment.NEUTRAL,
    (MacroRegime.NEUTRAL, DonatienLean.MIXED): Alignment.NEUTRAL,
}


class AlignmentAssessment(BaseModel):
    """One deterministic alignment read. Every field needed to answer "why
    was this ALIGNED/CONFLICT/NEUTRAL/INSUFFICIENT_DATA" is retained here
    directly. `market_*`/`donatien_*` fields are None whenever that source
    was unavailable (or, for market fields, in REVIEW) for this `as_of`."""

    as_of: date
    alignment: Alignment
    methodology_version: str = ALIGNMENT_METHODOLOGY_VERSION

    market_regime: MacroRegime | None
    market_regime_coverage: float | None
    market_as_of: date | None

    donatien_lean: DonatienLean | None
    donatien_dominant_regime: str | None  # audit-only, never load-bearing
    donatien_confidence: str | None  # audit-only, never load-bearing
    donatien_defensiveness: float | None  # audit-only, never load-bearing
    donatien_scenario_weights: dict[str, float] | None
    donatien_run_date: date | None
    donatien_source_observed_at: datetime | None
    donatien_retrieved_at: datetime | None


def build_alignment_assessment(
    *,
    as_of: date,
    market: MacroAssessment | None,
    donatien: DonatienCalibration | None,
    donatien_retrieved_at: datetime | None = None,
) -> AlignmentAssessment:
    """Pure computation: no network, no database. `market`/`donatien` are
    the already-computed/already-fetched evidence for this `as_of` --
    fetching the point-in-time-correct pair is the caller's
    (`alpha_lab.alignment.service`) responsibility, not this function's.

    Either input missing, market in REVIEW, or a Donatien scenario
    taxonomy AlphaLab hasn't verified all resolve to INSUFFICIENT_DATA --
    never a fabricated ALIGNED/CONFLICT/NEUTRAL.
    """
    market_regime = None if market is None else market.regime
    market_regime_coverage = None if market is None else market.coverage
    market_as_of = None if market is None else market.as_of

    donatien_lean = None if donatien is None else classify_donatien_lean(donatien.scenario_weights)

    if market is None or donatien is None or market_regime == MacroRegime.REVIEW or donatien_lean == DonatienLean.UNKNOWN:
        alignment = Alignment.INSUFFICIENT_DATA
    else:
        alignment = _ALIGNMENT_TABLE[(market_regime, donatien_lean)]

    return AlignmentAssessment(
        as_of=as_of,
        alignment=alignment,
        market_regime=market_regime,
        market_regime_coverage=market_regime_coverage,
        market_as_of=market_as_of,
        donatien_lean=donatien_lean,
        donatien_dominant_regime=None if donatien is None else donatien.dominant_regime,
        donatien_confidence=None if donatien is None else donatien.confidence,
        donatien_defensiveness=None if donatien is None else donatien.defensiveness,
        donatien_scenario_weights=None if donatien is None else donatien.scenario_weights,
        donatien_run_date=None if donatien is None else donatien.run_date,
        donatien_source_observed_at=None if donatien is None else source_observed_at(donatien),
        donatien_retrieved_at=donatien_retrieved_at,
    )
