"""Deterministic, offline tests for alpha_lab.conviction (roadmap Phase 8).

Every object is hand-built: no network, no database except the one
integration test at the bottom. The gates from the package docstring each
have a dedicated test.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
from conviction_fixtures import (
    COVERAGE,
    EVALUATION,
    STALE_DAYS,
    _alignment,
    _assess,
    _family,
    _research,
    _state,
)

from alpha_lab.alignment.alignment import DonatienLean
from alpha_lab.conviction import (
    Agreement,
    Conflict,
    ConflictKind,
    ConflictStrength,
    ConvictionAssessment,
    Direction,
    EvidenceSide,
    Family,
    FamilyAssessment,
    FamilyLean,
    FamilyStatus,
    assess_conviction,
)
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research.ai_rating import (
    AIDimensionValue,
)
from alpha_lab.research.analyst_consensus import AnalystRating
from alpha_lab.research.analyst_research import (
    RevisionDirection,
)
from alpha_lab.research.technical import TechnicalRating
from alpha_lab.research_state import ResearchState

# --- agreement truth table --------------------------------------------------

def test_three_established_families_agreeing_is_convergent():
    result = _assess(_research())
    assert result.agreement is Agreement.CONVERGENT and result.direction is Direction.POSITIVE
    assert result.conflicts == []
    assert [f.status for f in result.families] == [FamilyStatus.ESTABLISHED] * 3


def test_convergence_also_holds_on_the_negative_side():
    result = _assess(_research(
        fundamental=20.0, consensus=AnalystRating.SELL, revisions=RevisionDirection.DETERIORATING,
        technical=TechnicalRating.SELL,
    ))
    assert result.agreement is Agreement.CONVERGENT and result.direction is Direction.NEGATIVE


def test_a_provisional_family_can_join_an_agreement_but_not_establish_it():
    """Reviewer's case: thin evidence must not carry the weight of full evidence."""
    result = _assess(_research(technical_coverage=0.52))
    assert _family(result, Family.PRICE_BEHAVIOUR).status is FamilyStatus.PROVISIONAL
    assert result.agreement is Agreement.LEANING and result.direction is Direction.POSITIVE
    assert _assess(_research(technical_coverage=0.90)).agreement is Agreement.CONVERGENT


def test_two_agreeing_families_with_the_third_unassessed_is_leaning():
    result = _assess(_research(technical=None))
    assert _family(result, Family.PRICE_BEHAVIOUR).status is FamilyStatus.UNASSESSED
    assert result.agreement is Agreement.LEANING and result.direction is Direction.POSITIVE


def test_opposite_established_families_are_a_hard_conflict_and_contested():
    result = _assess(_research(fundamental=20.0))  # fundamentals negative vs market/price positive
    assert result.agreement is Agreement.CONTESTED and result.direction is Direction.NONE
    hard = [c for c in result.conflicts if c.strength is ConflictStrength.HARD]
    assert hard and all(c.kind is ConflictKind.CROSS_FAMILY and c.rule_id == "X1-cross-family" for c in hard)
    sides = hard[0].sides
    assert {side.provenance_id for side in sides} <= {"prov-fundamentals", "prov-analyst_activity", "prov-technicals"}
    assert all(side.label and side.status for side in sides)


def test_a_conflict_with_a_provisional_side_is_soft_and_not_contested():
    result = _assess(_research(fundamental=20.0, fundamental_coverage=0.6))
    assert _family(result, Family.FUNDAMENTAL).status is FamilyStatus.PROVISIONAL
    assert {c.strength for c in result.conflicts} == {ConflictStrength.SOFT}
    assert result.agreement is Agreement.MIXED and result.direction is Direction.NONE


def test_consensus_against_revisions_is_one_family_in_intra_family_conflict():
    result = _assess(_research(consensus=AnalystRating.BUY, revisions=RevisionDirection.DETERIORATING))
    market = _family(result, Family.MARKET_OPINION)
    assert market.lean is FamilyLean.CONFLICTED  # casts no vote
    intra = [c for c in result.conflicts if c.kind is ConflictKind.INTRA_FAMILY]
    assert len(intra) == 1 and intra[0].strength is ConflictStrength.HARD
    assert result.agreement is Agreement.CONTESTED


def test_a_neutral_member_does_not_oppose_a_definite_one():
    result = _assess(_research(consensus=AnalystRating.NEUTRAL, revisions=RevisionDirection.DETERIORATING))
    assert _family(result, Family.MARKET_OPINION).lean is FamilyLean.NEGATIVE
    assert not [c for c in result.conflicts if c.kind is ConflictKind.INTRA_FAMILY]


def test_a_missing_member_makes_the_family_provisional_not_established():
    result = _assess(_research(revisions=None))
    market = _family(result, Family.MARKET_OPINION)
    assert market.status is FamilyStatus.PROVISIONAL and market.lean is FamilyLean.POSITIVE
    assert result.agreement is Agreement.LEANING


def test_all_neutral_is_neutral_and_a_single_directional_family_is_not_agreement():
    neutral = _assess(_research(
        fundamental=55.0, consensus=AnalystRating.NEUTRAL, revisions=RevisionDirection.STABLE,
        technical=TechnicalRating.NEUTRAL,
    ))
    assert neutral.agreement is Agreement.NEUTRAL and neutral.direction is Direction.NONE
    lone = _assess(_research(
        fundamental=55.0, consensus=AnalystRating.NEUTRAL, revisions=RevisionDirection.STABLE,
        technical=TechnicalRating.BUY,
    ))
    assert lone.agreement is Agreement.MIXED and lone.direction is Direction.NONE


# --- sufficiency: unassessed is never guessed -------------------------------

def test_an_etf_has_no_fundamentals_family_and_is_insufficient_on_momentum_alone():
    result = _assess(_research(applicable=False, consensus=None, revisions=None, technical=TechnicalRating.BUY))
    fundamentals = _family(result, Family.FUNDAMENTAL)
    assert fundamentals.status is FamilyStatus.UNASSESSED and "not applicable" in fundamentals.reason
    assert result.agreement is Agreement.INSUFFICIENT and result.direction is Direction.NONE


def test_review_and_missing_evidence_are_unassessed_with_no_lean():
    result = _assess(_research(technical=TechnicalRating.REVIEW, consensus=None, revisions=None))
    for family in (Family.PRICE_BEHAVIOUR, Family.MARKET_OPINION):
        assessed = _family(result, family)
        assert assessed.status is FamilyStatus.UNASSESSED and assessed.lean is FamilyLean.NONE
    assert _assess(None).agreement is Agreement.INSUFFICIENT


def test_fundamentals_below_the_minimum_coverage_are_unassessed():
    result = _assess(_research(fundamental_coverage=0.30))
    assert _family(result, Family.FUNDAMENTAL).status is FamilyStatus.UNASSESSED


def test_stale_price_evidence_is_provisional():
    stale = _assess(_research(price_through=EVALUATION - timedelta(days=STALE_DAYS + 1)))
    assert _family(stale, Family.PRICE_BEHAVIOUR).status is FamilyStatus.PROVISIONAL
    assert "older than" in _family(stale, Family.PRICE_BEHAVIOUR).reason
    fresh = _assess(_research(price_through=EVALUATION - timedelta(days=STALE_DAYS)))
    assert _family(fresh, Family.PRICE_BEHAVIOUR).status is FamilyStatus.ESTABLISHED


def test_freshness_follows_the_newest_price_bar_not_the_compute_date():
    """Review finding: technicals rebuilt today from old stored prices carry
    today's `as_of` -- they must not read as fresh."""
    research = _research(price_through=EVALUATION - timedelta(days=30))
    assert research.technical_summary.as_of == EVALUATION  # computed "today"
    result = _assess(research)
    assert _family(result, Family.PRICE_BEHAVIOUR).status is FamilyStatus.PROVISIONAL
    assert result.agreement is Agreement.LEANING  # a stale family cannot establish CONVERGENT


def test_an_unrecorded_price_date_is_never_assumed_fresh():
    result = _assess(_research(price_through=None))  # e.g. a summary persisted before the field existed
    price = _family(result, Family.PRICE_BEHAVIOUR)
    assert price.status is FamilyStatus.PROVISIONAL and "not recorded" in price.reason
    assert result.agreement is Agreement.LEANING


def test_a_hard_conflict_is_surfaced_even_when_only_one_family_is_assessed():
    """Review finding: market opinion alone (no fundamentals, no technicals)
    with consensus vs revisions in established disagreement is CONTESTED, not
    INSUFFICIENT -- the detected contest must not be hidden."""
    result = _assess(_research(
        applicable=False, technical=None, consensus=AnalystRating.BUY, revisions=RevisionDirection.DETERIORATING,
    ))
    assert [f.status for f in result.families].count(FamilyStatus.UNASSESSED) == 2
    assert result.agreement is Agreement.CONTESTED and result.direction is Direction.NONE
    assert [c.kind for c in result.conflicts] == [ConflictKind.INTRA_FAMILY]
    # ... while no conflict and one assessed family stays INSUFFICIENT
    assert _assess(_research(applicable=False, technical=None, revisions=None)).agreement is Agreement.INSUFFICIENT


# --- independence ------------------------------------------------------------

def test_the_fundamental_family_ignores_momentum_revisions_and_ai_categories():
    """The composite score is 15% revisions + 15% momentum + 10% AI; letting
    those into the Fundamental family would double count the other families."""
    low_extras = _assess(_research(fundamental=20.0, extras=0.0))
    high_extras = _assess(_research(fundamental=20.0, extras=100.0))
    assert _family(low_extras, Family.FUNDAMENTAL).lean is FamilyLean.NEGATIVE
    assert _family(high_extras, Family.FUNDAMENTAL).model_dump() == _family(low_extras, Family.FUNDAMENTAL).model_dump()


def test_fundamentals_lean_is_the_sign_of_positive_minus_negative_and_ties_are_neutral():
    positive = _assess(_research(fundamental=[80, 80, 80, 20, 20]))
    assert _family(positive, Family.FUNDAMENTAL).lean is FamilyLean.POSITIVE
    assert _family(positive, Family.FUNDAMENTAL).reason == "3 positive / 2 negative / 0 neutral"
    tied = _assess(_research(fundamental=[80, 80, 20, 20, 55]))
    assert _family(tied, Family.FUNDAMENTAL).lean is FamilyLean.NEUTRAL


def test_the_ai_rating_and_composite_never_vote_they_only_flag_divergence():
    base = _assess(_research())
    for ai in (AIDimensionValue.VERY_POSITIVE, AIDimensionValue.VERY_NEGATIVE, AIDimensionValue.REVIEW):
        varied = _assess(_research(ai=ai))
        assert [f.model_dump() for f in varied.families] == [f.model_dump() for f in base.families]
        assert (varied.agreement, varied.direction) == (base.agreement, base.direction)
    opposed = _assess(_research(ai=AIDimensionValue.NEGATIVE, interpretation="Weak"))
    flags = [c for c in opposed.conflicts if c.kind is ConflictKind.SYNTHESIS_DIVERGENCE]
    assert {flag.sides[0].domain for flag in flags} == {"ai_research", "composite_score"}
    assert all(flag.strength is ConflictStrength.CONTEXT for flag in flags)
    assert opposed.agreement is Agreement.CONVERGENT  # unchanged by the flags
    assert [side.domain for side in opposed.synthesis] == ["ai_research", "composite_score"]


def test_regime_context_flags_conflict_with_the_security_but_never_votes():
    risk_off = _assess(_research(), alignment=_alignment(MacroRegime.RISK_OFF, DonatienLean.DEFENSIVE))
    flags = [c for c in risk_off.conflicts if c.kind is ConflictKind.REGIME_VS_SECURITY]
    assert len(flags) == 2 and all(f.strength is ConflictStrength.CONTEXT for f in flags)
    assert risk_off.agreement is Agreement.CONVERGENT and len(risk_off.context) == 2
    risk_on = _assess(_research(), alignment=_alignment(MacroRegime.RISK_ON, DonatienLean.CONSTRUCTIVE))
    assert not [c for c in risk_on.conflicts if c.kind is ConflictKind.REGIME_VS_SECURITY]
    assert _assess(_research()).context == []  # no alignment evidence: nothing invented


def test_regime_flags_cite_the_alignment_field_they_were_read_from():
    """Review finding: both regime lines come from the alignment assessment;
    attaching `state.macro`'s provenance would cite a different snapshot."""
    result = _assess(_research(), alignment=_alignment(MacroRegime.RISK_OFF, DonatienLean.DEFENSIVE))
    assert {side.provenance_id for side in result.context} == {"prov-donatien_alignment"}
    flags = [c for c in result.conflicts if c.kind is ConflictKind.REGIME_VS_SECURITY]
    assert {side.provenance_id for flag in flags for side in flag.sides} == {"prov-donatien_alignment"}


def test_context_flags_need_a_direction():
    contested = _assess(_research(fundamental=20.0, ai=AIDimensionValue.NEGATIVE),
                        alignment=_alignment(MacroRegime.RISK_OFF))
    assert contested.direction is Direction.NONE
    assert not [c for c in contested.conflicts if c.strength is ConflictStrength.CONTEXT]


# --- gates -------------------------------------------------------------------

def test_no_score_weight_or_probability_field_exists_anywhere_in_the_output():
    forbidden = ("score", "weight", "probab", "conviction", "confidence", "average")
    float_fields = []
    for model in (ConvictionAssessment, FamilyAssessment, Conflict, EvidenceSide):
        for name, info in model.model_fields.items():
            assert not any(word in name for word in forbidden), f"{model.__name__}.{name}"
            if "float" in str(info.annotation):
                float_fields.append(f"{model.__name__}.{name}")
    assert float_fields == ["FamilyAssessment.coverage"]  # measured evidence coverage only


def test_the_assessment_is_deterministic_and_json_serializable():
    state = _state(_research(consensus=AnalystRating.BUY, revisions=RevisionDirection.DETERIORATING),
                   alignment=_alignment(MacroRegime.RISK_OFF))
    first = assess_conviction(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    second = assess_conviction(state, coverage=COVERAGE, stale_price_days=STALE_DAYS)
    assert first.model_dump_json() == second.model_dump_json()
    assert ConvictionAssessment.model_validate_json(first.model_dump_json()) == first


def test_no_scoring_ranking_backtest_or_portfolio_module_imports_the_conviction_layer():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for directory in ("research", "screener", "strategy", "backtest", "portfolio", "ratings", "factors", "scorecard"):
        for path in (root / "alpha_lab" / directory).rglob("*.py"):
            if "alpha_lab.conviction" in path.read_text():
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"scoring/ranking code must never import the conviction layer: {offenders}"


# --- integration through get_research_state ---------------------------------

def test_get_research_state_fills_conflicts_and_conviction_without_fabrication():
    from alpha_lab.config import load_settings
    from alpha_lab.database import create_schema, make_engine
    from alpha_lab.research_state import get_research_state

    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    state = get_research_state(engine, load_settings(), "NOSUCH")
    assert state.conflicts == []  # computed (empty), no longer the reserved None
    assert state.conviction["agreement"] == "INSUFFICIENT" and state.conviction["direction"] == "NONE"
    assert all(f["status"] == "UNASSESSED" for f in state.conviction["families"])
    ResearchState.model_validate_json(state.model_dump_json())  # stays serializable


@pytest.mark.parametrize("lean", list(FamilyLean))
def test_every_family_lean_has_a_stable_string_value(lean):
    assert lean.value == lean.name


def test_revisions_whose_periods_disagree_are_provisional_and_cannot_anchor_a_hard_conflict():
    """Real data (AMZN, META, JPM): the nearest fiscal period is DETERIORATING
    while the next is IMPROVING. One period of a split reading is not an
    established direction."""
    research = _research(consensus=AnalystRating.BUY, revisions=RevisionDirection.DETERIORATING)
    later = research.analyst_research.revision_trend[0].model_copy(update={
        "fiscal_period": date(2027, 12, 31), "direction": RevisionDirection.IMPROVING,
    })
    research = research.model_copy(update={
        "analyst_research": research.analyst_research.model_copy(update={
            "revision_trend": [research.analyst_research.revision_trend[0], later],
        }),
    })
    result = _assess(research)
    market = _family(result, Family.MARKET_OPINION)
    assert market.lean is FamilyLean.CONFLICTED and market.status is FamilyStatus.PROVISIONAL
    intra = [c for c in result.conflicts if c.kind is ConflictKind.INTRA_FAMILY]
    assert len(intra) == 1 and intra[0].strength is ConflictStrength.SOFT
    assert "revision periods disagree" in market.reason
    assert result.agreement is not Agreement.CONTESTED  # no HARD conflict: fundamentals/price agree here
    # ... while consistent periods keep the HARD intra-family conflict
    consistent = _assess(_research(consensus=AnalystRating.BUY, revisions=RevisionDirection.DETERIORATING))
    assert [c.strength for c in consistent.conflicts if c.kind is ConflictKind.INTRA_FAMILY] == [ConflictStrength.HARD]


def test_a_failing_conviction_layer_never_takes_down_the_research_state(monkeypatch, caplog):
    """Review finding: get_research_state used to be a pure assembler of
    independent fields; the additive layer must not be able to break it."""
    import logging

    import alpha_lab.research_state as research_state_module
    from alpha_lab.config import load_settings
    from alpha_lab.database import create_schema, make_engine

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected enum")

    monkeypatch.setattr(research_state_module, "assess_conviction", boom)
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with caplog.at_level(logging.ERROR, logger="alpha_lab.research_state"):
        state = research_state_module.get_research_state(engine, load_settings(), "NOSUCH")
    assert state.ticker == "NOSUCH" and state.conviction is None and state.conflicts is None
    assert any("conviction assessment failed" in record.getMessage() for record in caplog.records)
