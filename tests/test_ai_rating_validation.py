"""Tests for alpha_lab.analytics.ai_rating_validation (roadmap Phase 7): the
frozen AI Rating validation protocol. The synthetic-world tests build price
paths and candidate snapshots directly (no database) so each criterion and
status can be driven precisely; the DB tests cover methodology grouping,
REVIEW exclusion and read-only behavior against real persisted snapshots."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.analytics.ai_rating_validation import (
    AI_RATING_VALIDATION_PROTOCOL_V1,
    AIMethodologyKey,
    AIRatingValidationProtocol,
    CriterionResult,
    ValidationObservation,
    ValidationStatus,
    _Candidate,
    _confidence_split,
    analyze_horizon,
    build_validation_report,
    incremental_association,
    select_independent_observations,
    validate_ai_rating,
)
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import CurrentAIResearchAssessment, Price, ResearchSnapshot, Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import CATEGORY_ORDER, ResearchService
from alpha_lab.research.ai_rating import AIDimensionAssessment, AIDimensionValue, AIEvidenceCoverage, AIResearchAssessment
from alpha_lab.screener import LiveResearchRecord

PROTOCOL = AI_RATING_VALIDATION_PROTOCOL_V1
KEY = AIMethodologyKey("ai-research-rating-v3", "deterministic-rule-based", "category-threshold-v2", "prompt-v1")


# --- the protocol is frozen ------------------------------------------------


def test_protocol_v1_values_are_pinned():
    """A threshold change is a new protocol version, never an edit of V1.
    If this fails, add a V2 instead of changing V1."""
    assert PROTOCOL == AIRatingValidationProtocol(
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
    with pytest.raises(FrozenInstanceError):
        PROTOCOL.min_distinct_tickers = 5


# --- synthetic worlds ------------------------------------------------------


def _prices(drift: float, n_days: int, rng) -> pd.Series:
    log_returns = drift + rng.normal(0, 0.003, n_days)
    return pd.Series(
        100 * np.exp(np.cumsum(log_returns)), index=pd.date_range("2026-01-01", periods=n_days, freq="D")
    )


def _world(signal_fn, *, n_tickers=12, n_days=300, seed=7):
    """`signal_fn(drift, rng) -> (ai_score, deterministic_score, confidence)`.
    Ticker drifts span -0.4%..+0.4% per day, so a signal tracking drift is
    genuinely predictive of forward returns at every horizon."""
    rng = np.random.default_rng(seed)
    prices: dict[str, pd.Series] = {}
    candidates: list[_Candidate] = []
    for i, drift in enumerate(np.linspace(-0.004, 0.004, n_tickers)):
        ticker = f"T{i:02d}"
        prices[ticker] = _prices(drift, n_days, rng)
        for k in range(n_days - 1):
            day = prices[ticker].index[k].date()
            ai, det, confidence = signal_fn(drift, rng)
            candidates.append(
                _Candidate(
                    ticker=ticker, created_at=datetime.combine(day, time(12)), as_of=day,
                    snapshot_id=f"{ticker}-{k}", ai_score=ai, deterministic_score=det,
                    confidence=confidence, research_refresh_version_id=None,
                )
            )
    return candidates, prices


def _confidence(rng) -> float:
    return 0.9 if rng.random() < 0.5 else 0.5


def _informative(drift, rng):
    return 50 + 1000 * drift + rng.normal(0, 3), rng.uniform(0, 100), _confidence(rng)


def _more_informative_when_confidence_is_low(drift, rng):
    """Predictive overall, but the *low*-confidence assessments are the
    sharper ones -- i.e. `confidence` is miscalibrated."""
    if rng.random() < 0.5:
        return 50 + 1000 * drift + rng.normal(0, 2), rng.uniform(0, 100), 0.5
    return 50 + 1000 * drift + rng.normal(0, 6), rng.uniform(0, 100), 0.9


def _pure_noise(drift, rng):
    return rng.uniform(0, 100), 50 + 1000 * drift, _confidence(rng)


def _redundant_with_deterministic(drift, rng):
    deterministic = 50 + 1000 * drift
    return deterministic + rng.normal(0, 0.5), deterministic, _confidence(rng)


def _criterion(report, name):
    return next(c for c in report.criteria if c.name == name)


# --- horizon-specific independence -----------------------------------------


def _flat_prices(n_days=200) -> pd.Series:
    return pd.Series(np.linspace(100, 200, n_days), index=pd.date_range("2026-01-01", periods=n_days, freq="D"))


def _daily_candidates(ticker, n, *, start=date(2026, 1, 1)):
    return [
        _Candidate(
            ticker=ticker, created_at=datetime.combine(start + timedelta(days=i), time(12)),
            as_of=start + timedelta(days=i), snapshot_id=f"{ticker}-{i}", ai_score=50.0,
            deterministic_score=50.0, confidence=0.8, research_refresh_version_id=None,
        )
        for i in range(n)
    ]


def _entry_positions(prices: pd.Series, observations) -> list[int]:
    return [int(prices.index.searchsorted(pd.Timestamp(o.as_of), side="right")) for o in observations]


def test_independence_spacing_scales_with_the_horizon():
    """The same daily snapshots yield far fewer independent observations
    for a longer horizon -- 30 'observations' at 5 days are not 30 at 60."""
    prices = {"AAA": _flat_prices()}
    candidates = _daily_candidates("AAA", 120)
    kept = {h: select_independent_observations(candidates, prices, h) for h in (5, 20, 60)}
    assert len(kept[5]) > len(kept[20]) > len(kept[60]) >= 1
    for horizon, observations in kept.items():
        positions = _entry_positions(prices["AAA"], observations)
        assert all(b - a >= horizon for a, b in zip(positions, positions[1:]))


def test_same_day_snapshot_cluster_counts_once():
    prices = {"AAA": _flat_prices()}
    base = _daily_candidates("AAA", 1)[0]
    cluster = [
        _Candidate(**{**base.__dict__, "created_at": base.created_at + timedelta(minutes=m), "snapshot_id": f"s{m}"})
        for m in (0, 45, 90)
    ]
    assert len(select_independent_observations(cluster, prices, 5)) == 1


def test_weekly_snapshots_still_overlap_at_a_long_horizon():
    """One snapshot per calendar week is not independence for a 60-day
    return: their windows overlap almost entirely, so most are dropped."""
    prices = {"AAA": _flat_prices(400)}
    weekly = [c for i, c in enumerate(_daily_candidates("AAA", 300)) if i % 7 == 0]
    kept_60 = select_independent_observations(weekly, prices, 60)
    assert len(kept_60) < len(weekly) / 5
    positions = _entry_positions(prices["AAA"], kept_60)
    assert all(b - a >= 60 for a, b in zip(positions, positions[1:]))


# --- statistics appear only for testable horizons --------------------------


def test_below_the_floor_a_horizon_reports_counts_and_no_statistics():
    observations = [
        ValidationObservation("A", date(2026, 1, 1), f"s{i}", 50.0 + i, 50.0, 0.8, None, 0.01 * i) for i in range(10)
    ]
    result = analyze_horizon(observations, 20, PROTOCOL)
    assert not result.testable
    assert result.independent_observations == 10
    assert result.pearson is None and result.spearman is None
    assert result.incremental is None and result.confidence_split is None


def test_enough_observations_but_too_few_tickers_is_not_testable():
    observations = [
        ValidationObservation(f"A{i % 3}", date(2026, 1, 1), f"s{i}", 50.0 + i, 50.0, 0.8, None, 0.01 * i)
        for i in range(40)
    ]
    result = analyze_horizon(observations, 20, PROTOCOL)
    assert result.independent_observations == 40 and result.distinct_tickers == 3
    assert not result.testable


# --- incremental association analysis --------------------------------------


def test_incremental_association_detects_information_beyond_the_deterministic_score():
    rng = np.random.default_rng(3)
    n = 200
    ai = rng.normal(0, 1, n)
    det = rng.normal(0, 1, n)
    y = 0.02 * ai + rng.normal(0, 0.02, n)
    result = incremental_association(list(det), list(ai), list(y))
    assert result is not None
    assert result.ai_beta > 0 and result.ai_t_stat > 2
    assert abs(result.deterministic_t_stat) < 3


def test_incremental_association_finds_nothing_when_ai_only_restates_the_deterministic_score():
    rng = np.random.default_rng(3)
    n = 200
    det = rng.normal(0, 1, n)
    ai = det + rng.normal(0, 0.5, n)
    y = 0.02 * det + rng.normal(0, 0.02, n)
    result = incremental_association(list(det), list(ai), list(y))
    assert result is not None
    assert result.ai_t_stat < PROTOCOL.incremental_min_t_stat


def test_incremental_association_degenerate_inputs_return_none():
    assert incremental_association([1, 2, 3], [1, 2, 3], [0.1, 0.2, 0.3]) is None  # n < 4
    assert incremental_association([1.0] * 10, list(range(10)), [0.1] * 10) is None  # constant predictor
    assert incremental_association(list(range(10)), [5.0] * 10, list(range(10))) is None


# --- confidence split ------------------------------------------------------


def _split_observations(n_high, n_low, *, high_slope, low_slope, seed=1, noise=0.05):
    rng = np.random.default_rng(seed)
    obs = []
    for confidence, n, slope in ((0.9, n_high, high_slope), (0.5, n_low, low_slope)):
        for i in range(n):
            score = rng.uniform(0, 100)
            obs.append(
                ValidationObservation(
                    "A", date(2026, 1, 1), f"{confidence}-{i}", score, None, confidence, None,
                    slope * score + rng.normal(0, noise),
                )
            )
    return obs


def test_confidence_split_passes_only_when_high_confidence_is_meaningfully_stronger():
    strong_high = _confidence_split(_split_observations(40, 40, high_slope=0.01, low_slope=0.0), 20, PROTOCOL)
    assert strong_high.result == CriterionResult.PASS
    same_strength = _confidence_split(_split_observations(40, 40, high_slope=0.01, low_slope=0.01), 20, PROTOCOL)
    assert same_strength.result == CriterionResult.FAIL


def test_confidence_split_rejects_a_gap_that_is_not_statistically_distinguishable():
    """Two equally-informative buckets whose sample Spearmans still differ
    by more than the fixed gap must not pass on that gap alone (this seed
    yields a 0.13 gap that a fixed-gap rule would have accepted)."""
    observations = _split_observations(20, 20, high_slope=0.01, low_slope=0.01, seed=1, noise=0.6)
    split = _confidence_split(observations, 20, PROTOCOL)
    assert split.high_spearman - split.low_spearman >= PROTOCOL.min_confidence_spearman_difference
    assert split.result == CriterionResult.FAIL


def test_confidence_split_with_a_tiny_bucket_is_not_testable_not_a_verdict():
    split = _confidence_split(_split_observations(40, 5, high_slope=0.01, low_slope=0.0), 20, PROTOCOL)
    assert split.result == CriterionResult.NOT_TESTABLE
    assert split.high_spearman is None and split.low_spearman is None


# --- report statuses -------------------------------------------------------


def test_no_scored_assessments_is_not_testable():
    report = build_validation_report(KEY, [], {}, review_excluded=4)
    assert report.status == ValidationStatus.NOT_TESTABLE
    assert report.review_excluded == 4 and report.scored_snapshots == 0


def test_sparse_history_is_insufficient_history_with_no_statistics():
    candidates, prices = _world(_informative, n_tickers=3, n_days=40)
    report = build_validation_report(KEY, candidates, prices)
    assert report.status == ValidationStatus.INSUFFICIENT_HISTORY
    assert all(h.pearson is None for h in report.horizons)
    assert _criterion(report, "A_min_history").result == CriterionResult.NOT_TESTABLE


def test_a_single_testable_horizon_is_testable_no_validation():
    candidates, prices = _world(_informative, n_tickers=10, n_days=40)
    report = build_validation_report(KEY, candidates, prices)
    assert [h.testable for h in report.horizons] == [True, False, False]
    assert report.status == ValidationStatus.TESTABLE_NO_VALIDATION


def test_an_informative_signal_is_validation_supported():
    candidates, prices = _world(_informative)
    report = build_validation_report(KEY, candidates, prices)
    assert all(h.testable for h in report.horizons)
    assert report.status == ValidationStatus.VALIDATION_SUPPORTED
    for name in ("A_min_history", "B_association_consistency", "C_horizon_stability", "D_incremental_information"):
        assert _criterion(report, name).result == CriterionResult.PASS, name


def test_a_noise_signal_is_not_supported():
    candidates, prices = _world(_pure_noise)
    report = build_validation_report(KEY, candidates, prices)
    assert _criterion(report, "A_min_history").result == CriterionResult.PASS
    assert report.status == ValidationStatus.VALIDATION_NOT_SUPPORTED
    assert _criterion(report, "C_horizon_stability").result == CriterionResult.FAIL


def test_a_signal_that_only_restates_the_deterministic_score_is_not_supported():
    """Predictive on its own (criterion C passes), but adds nothing beyond
    the deterministic score (criterion D fails) -- must not validate."""
    candidates, prices = _world(_redundant_with_deterministic)
    report = build_validation_report(KEY, candidates, prices)
    assert _criterion(report, "C_horizon_stability").result == CriterionResult.PASS
    assert _criterion(report, "D_incremental_information").result == CriterionResult.FAIL
    assert report.status == ValidationStatus.VALIDATION_NOT_SUPPORTED


def test_missing_deterministic_scores_make_criterion_d_not_testable_not_failed():
    candidates, prices = _world(_informative)
    for candidate in candidates:
        candidate.deterministic_score = None
    report = build_validation_report(KEY, candidates, prices)
    assert _criterion(report, "D_incremental_information").result == CriterionResult.NOT_TESTABLE
    assert report.status == ValidationStatus.VALIDATION_NOT_SUPPORTED  # D not PASS -> cannot validate


def test_confidence_calibration_is_informational_and_never_gates_the_status():
    candidates, prices = _world(_more_informative_when_confidence_is_low)
    report = build_validation_report(KEY, candidates, prices)
    assert _criterion(report, "E_confidence_calibration").result == CriterionResult.FAIL
    assert report.status == ValidationStatus.VALIDATION_SUPPORTED


def test_report_is_reproducible_and_carries_no_combined_score():
    candidates, prices = _world(_informative, n_tickers=10, n_days=120)
    first = build_validation_report(KEY, candidates, prices)
    second = build_validation_report(KEY, candidates, prices)
    assert first.status == second.status
    assert first.horizons == second.horizons
    assert first.criteria == second.criteria
    assert first.protocol_version == "ai-rating-validation-v1"
    assert not hasattr(first, "score") and not hasattr(first, "validation_score")


# --- database: methodology grouping, REVIEW exclusion, read-only -----------


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ai_rating_validation.db'}")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _record(ticker: str, evaluation_date: date) -> LiveResearchRecord:
    return LiveResearchRecord(
        ticker=ticker, company=f"{ticker} Inc", price=100.0, market_cap=1_000.0,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="equity", themes=[], ethical_status="PASS", data_quality_status="valid",
        overall_score=70.0, overall_rank=1,
        category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={}, percentile_metrics={}, overall_live_coverage=0.5,
        quantitative_coverage=0.5, ai_coverage=0.0, historical_coverage=0.0,
        confidence="Moderate", provenance={}, last_refreshed=None,
        rating_version="test-v1", configuration_hash="test-config", evaluation_date=evaluation_date,
    )


def _assessment(ticker: str, score: float | None, *, model: str, confidence=0.8) -> AIResearchAssessment:
    dimension = AIDimensionAssessment(value=AIDimensionValue.POSITIVE, confidence=0.5)
    coverage = AIEvidenceCoverage(
        fundamental_coverage=1.0, analyst_coverage=1.0, technical_coverage=1.0, overall_ai_evidence_coverage=1.0
    )
    return AIResearchAssessment(
        ticker=ticker, score=score,
        rating=AIDimensionValue.REVIEW if score is None else AIDimensionValue.POSITIVE,
        confidence=confidence, dimensions={"business_outlook": dimension}, evidence_coverage=coverage,
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[], supporting_evidence=[],
        prompt_version="prompt-v1", model=model, model_fingerprint=None,
        research_schema_version="stockresearch-v2", generated_at=datetime.now(UTC), as_of=date(2026, 1, 1),
        source="deterministic-rule-based",
    )


def _seed_snapshot(engine, settings, ticker: str, created_at: datetime, assessment: AIResearchAssessment) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
    Phase3Repository(engine).save_current_research([_record(ticker, created_at.date())])
    service = ResearchService(engine, settings)
    research = service.get_stock_research(ticker).model_copy(update={"ai_research_assessment": assessment})
    summary = service.persist_snapshot(research)
    with Session(engine) as session:
        row = session.scalar(select(ResearchSnapshot).where(ResearchSnapshot.snapshot_id == summary.snapshot_id))
        row.created_at = created_at
        session.commit()


def _seed_prices(engine, ticker: str, n: int) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
        for i in range(n):
            session.add(Price(ticker=ticker, date=date(2026, 1, 1) + timedelta(days=i), close=100.0 + i))
        session.commit()


def test_reports_are_grouped_by_methodology_and_never_pooled(engine):
    settings = load_settings()
    _seed_prices(engine, "AAA", 60)
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 1, 3, 10), _assessment("AAA", 60.0, model="model-a"))
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 1, 20, 10), _assessment("AAA", 62.0, model="model-a"))
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 1, 25, 10), _assessment("AAA", 40.0, model="model-b"))
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 2, 1, 10), _assessment("AAA", None, model="model-a"))

    reports = validate_ai_rating(engine, settings, ["AAA"])
    by_model = {r.methodology.model: r for r in reports}
    assert set(by_model) == {"model-a", "model-b"}
    assert by_model["model-a"].scored_snapshots == 2 and by_model["model-a"].review_excluded == 1
    assert by_model["model-b"].scored_snapshots == 1 and by_model["model-b"].review_excluded == 0
    assert all(r.status == ValidationStatus.INSUFFICIENT_HISTORY for r in reports)


def test_a_methodology_with_only_review_assessments_is_not_testable(engine):
    settings = load_settings()
    _seed_prices(engine, "AAA", 30)
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 1, 3, 10), _assessment("AAA", None, model="model-a"))
    (report,) = validate_ai_rating(engine, settings, ["AAA"])
    assert report.status == ValidationStatus.NOT_TESTABLE
    assert report.review_excluded == 1


def test_no_ai_rated_snapshots_returns_no_reports(engine):
    assert validate_ai_rating(engine, load_settings(), ["AAA"]) == []


def test_same_day_snapshots_from_the_database_count_as_one_observation(engine):
    settings = load_settings()
    _seed_prices(engine, "AAA", 60)
    for minutes, score in ((0, 60.0), (45, 61.0), (90, 62.0)):
        _seed_snapshot(
            engine, settings, "AAA", datetime(2026, 1, 3, 10) + timedelta(minutes=minutes),
            _assessment("AAA", score, model="model-a"),
        )
    (report,) = validate_ai_rating(engine, settings, ["AAA"])
    assert report.scored_snapshots == 3
    assert len(report.observations[5]) == 1


def test_validation_is_read_only(engine):
    settings = load_settings()
    _seed_prices(engine, "AAA", 60)
    _seed_snapshot(engine, settings, "AAA", datetime(2026, 1, 3, 10), _assessment("AAA", 60.0, model="model-a"))

    def counts():
        with Session(engine) as session:
            return (
                session.scalar(select(func.count()).select_from(ResearchSnapshot)),
                session.scalar(select(func.count()).select_from(CurrentAIResearchAssessment)),
                session.scalar(select(func.count()).select_from(Price)),
            )

    before = counts()
    validate_ai_rating(engine, settings, ["AAA"])
    assert counts() == before
