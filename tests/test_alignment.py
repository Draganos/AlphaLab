"""Offline, deterministic tests for alpha_lab.alignment.alignment's pure
computation. No network access, no database."""

from datetime import date, datetime

import pytest

from alpha_lab.alignment.alignment import (
    Alignment,
    DonatienLean,
    build_alignment_assessment,
    classify_donatien_lean,
)
from alpha_lab.macro.regime import MacroAssessment, MacroRegime
from alpha_lab.providers.donatien import DonatienCalibration

_AS_OF = date(2024, 6, 1)


def _market(regime: MacroRegime, *, coverage: float = 1.0) -> MacroAssessment:
    return MacroAssessment(
        scope="US", regime=regime,
        regime_score=None if regime == MacroRegime.REVIEW else 0.0,
        confidence=coverage, coverage=coverage, indicators=[], as_of=_AS_OF, source="test",
    )


def _donatien(
    scenario_weights: dict[str, float],
    *,
    dominant_regime: str = "Some prose regime label",
    confidence: str = "Medium",
    defensiveness: float = 4.0,
) -> DonatienCalibration:
    return DonatienCalibration(
        run_date=_AS_OF, run_time="20:15", macro_report_date=_AS_OF,
        dominant_regime=dominant_regime, confidence=confidence,
        scenario_weights=scenario_weights, defensiveness=defensiveness,
        top_drivers=[], key_changes=[], trend_contrarian_split={}, tiers={},
    )


_CONSTRUCTIVE_WEIGHTS = {"Soft Landing": 60, "Reacceleration": 20, "Stagflation": 10, "Deflationary Bust": 10}
_DEFENSIVE_WEIGHTS = {"Stagflation": 40, "Deflationary Bust": 35, "Soft Landing": 15, "Reacceleration": 10}
_TIED_WEIGHTS = {"Stagflation": 25, "Deflationary Bust": 25, "Soft Landing": 25, "Reacceleration": 25}
_UNKNOWN_TAXONOMY_WEIGHTS = {"Some New Scenario": 100}


# --- classify_donatien_lean --------------------------------------------------


def test_classify_donatien_lean_constructive_when_growth_supportive_weight_dominates():
    assert classify_donatien_lean(_CONSTRUCTIVE_WEIGHTS) == DonatienLean.CONSTRUCTIVE


def test_classify_donatien_lean_defensive_when_growth_adverse_weight_dominates():
    assert classify_donatien_lean(_DEFENSIVE_WEIGHTS) == DonatienLean.DEFENSIVE


def test_classify_donatien_lean_mixed_on_an_exact_tie():
    assert classify_donatien_lean(_TIED_WEIGHTS) == DonatienLean.MIXED


def test_classify_donatien_lean_unknown_when_no_recognized_scenario_name_present():
    assert classify_donatien_lean(_UNKNOWN_TAXONOMY_WEIGHTS) == DonatienLean.UNKNOWN


def test_classify_donatien_lean_unknown_on_empty_scenario_weights():
    assert classify_donatien_lean({}) == DonatienLean.UNKNOWN


def test_classify_donatien_lean_ignores_unrecognized_extra_keys():
    """An extra, unrecognized scenario name alongside recognized ones must
    not change the classification of the recognized ones."""
    weights = dict(_CONSTRUCTIVE_WEIGHTS)
    weights["Some Extra Scenario"] = 1000
    assert classify_donatien_lean(weights) == DonatienLean.CONSTRUCTIVE


# --- build_alignment_assessment: mapping table -------------------------------


def test_risk_on_and_constructive_is_aligned():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS)
    )
    assert result.alignment == Alignment.ALIGNED


def test_risk_off_and_defensive_is_aligned():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_OFF), donatien=_donatien(_DEFENSIVE_WEIGHTS)
    )
    assert result.alignment == Alignment.ALIGNED


def test_risk_on_and_defensive_is_conflict():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=_donatien(_DEFENSIVE_WEIGHTS)
    )
    assert result.alignment == Alignment.CONFLICT


def test_risk_off_and_constructive_is_conflict():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_OFF), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS)
    )
    assert result.alignment == Alignment.CONFLICT


@pytest.mark.parametrize("weights", [_CONSTRUCTIVE_WEIGHTS, _DEFENSIVE_WEIGHTS, _TIED_WEIGHTS])
def test_neutral_market_is_always_neutral_regardless_of_donatien_lean(weights):
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.NEUTRAL), donatien=_donatien(weights)
    )
    assert result.alignment == Alignment.NEUTRAL


@pytest.mark.parametrize("regime", [MacroRegime.RISK_ON, MacroRegime.RISK_OFF])
def test_mixed_donatien_lean_is_neutral_regardless_of_market_direction(regime):
    result = build_alignment_assessment(as_of=_AS_OF, market=_market(regime), donatien=_donatien(_TIED_WEIGHTS))
    assert result.alignment == Alignment.NEUTRAL


# --- build_alignment_assessment: degradation / insufficient data ------------


def test_market_review_is_insufficient_data_even_with_a_valid_donatien_read():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.REVIEW), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS)
    )
    assert result.alignment == Alignment.INSUFFICIENT_DATA
    # audit context is still retained even though it wasn't used to decide
    assert result.market_regime == MacroRegime.REVIEW


def test_missing_market_is_insufficient_data():
    result = build_alignment_assessment(as_of=_AS_OF, market=None, donatien=_donatien(_CONSTRUCTIVE_WEIGHTS))
    assert result.alignment == Alignment.INSUFFICIENT_DATA
    assert result.market_regime is None


def test_missing_donatien_is_insufficient_data():
    result = build_alignment_assessment(as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=None)
    assert result.alignment == Alignment.INSUFFICIENT_DATA
    assert result.donatien_lean is None


def test_both_missing_is_insufficient_data():
    result = build_alignment_assessment(as_of=_AS_OF, market=None, donatien=None)
    assert result.alignment == Alignment.INSUFFICIENT_DATA


def test_unrecognized_donatien_scenario_taxonomy_is_insufficient_data_not_guessed():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=_donatien(_UNKNOWN_TAXONOMY_WEIGHTS)
    )
    assert result.alignment == Alignment.INSUFFICIENT_DATA
    assert result.donatien_lean == DonatienLean.UNKNOWN


# --- audit-only fields must never be load-bearing ----------------------------


def test_dominant_regime_confidence_and_defensiveness_never_affect_the_classification():
    """The brief explicitly forbids using Donatien's unstructured
    dominant_regime/confidence/defensiveness fields as classifier inputs --
    only scenario_weights may drive the categorical result. Changing them
    arbitrarily while holding scenario_weights fixed must not change
    `alignment`."""
    baseline = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON),
        donatien=_donatien(_CONSTRUCTIVE_WEIGHTS, dominant_regime="Deep Recession Panic",
                            confidence="Low", defensiveness=99.0),
    )
    contradictory_text = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON),
        donatien=_donatien(_CONSTRUCTIVE_WEIGHTS, dominant_regime="Everything is fine",
                            confidence="High", defensiveness=0.1),
    )
    assert baseline.alignment == contradictory_text.alignment == Alignment.ALIGNED
    # but the audit-only text is still faithfully retained, just not used
    assert baseline.donatien_dominant_regime == "Deep Recession Panic"
    assert contradictory_text.donatien_dominant_regime == "Everything is fine"


# --- determinism / auditability ---------------------------------------------


def test_same_inputs_always_produce_the_same_output():
    market = _market(MacroRegime.RISK_OFF)
    donatien = _donatien(_DEFENSIVE_WEIGHTS)
    first = build_alignment_assessment(as_of=_AS_OF, market=market, donatien=donatien)
    second = build_alignment_assessment(as_of=_AS_OF, market=market, donatien=donatien)
    assert first == second


def test_audit_fields_are_sufficient_to_reconstruct_the_verdict_without_recomputation():
    result = build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON, coverage=0.8), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS)
    )
    assert result.market_regime == MacroRegime.RISK_ON
    assert result.market_regime_coverage == pytest.approx(0.8)
    assert result.donatien_lean == DonatienLean.CONSTRUCTIVE
    assert result.donatien_scenario_weights == _CONSTRUCTIVE_WEIGHTS
    assert result.donatien_run_date == _AS_OF


def test_methodology_version_is_stamped():
    result = build_alignment_assessment(as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS))
    assert result.methodology_version == "alignment-v1"


def test_no_alignment_score_field_exists_on_the_model():
    """The brief forbids any numeric alignment/conviction score. Assert the
    model has no such field, so a future edit reintroducing one is caught."""
    forbidden = {"alignment_score", "conviction_score", "score", "weight", "confidence_score"}
    assert forbidden.isdisjoint(build_alignment_assessment(
        as_of=_AS_OF, market=_market(MacroRegime.RISK_ON), donatien=_donatien(_CONSTRUCTIVE_WEIGHTS)
    ).model_fields.keys())
