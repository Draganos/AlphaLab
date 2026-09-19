"""Tests for alpha_lab.analytics.signal_predictive_value: the read-only
signal/forward-return correlation study, never wired into any scoring or
backtest path.

Technical Summary's own indicator math is already exhaustively tested in
tests/test_technical_summary.py -- these tests monkeypatch
SupplementalResearchService.get_technical_summary_as_of to isolate this
module's own logic (pairing a signal value with the correct forward
return, skipping REVIEW samples, the correlation/significance math)
from that computation."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, ResearchSnapshot, Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import CATEGORY_ORDER, ResearchService
from alpha_lab.research.ai_rating import AIDimensionValue, AIEvidenceCoverage, AIResearchAssessment
from alpha_lab.research.analyst_consensus import AnalystConsensus
from alpha_lab.screener import LiveResearchRecord
from alpha_lab.analytics.signal_predictive_value import (
    MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE,
    CorrelationResult,
    InsufficientSnapshotHistory,
    SignalObservation,
    _correlate,
    collect_analyst_consensus_observations,
    collect_technical_summary_observations,
)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'signal_predictive_value.db'}")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_prices(engine, ticker: str, closes: list[float], *, start: date = date(2026, 1, 1)) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
        for i, close in enumerate(closes):
            session.add(Price(ticker=ticker, date=start + timedelta(days=i), close=close))
        session.commit()


# --- _correlate: pure math, no DB -------------------------------------------


def test_correlate_reports_perfect_positive_correlation():
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=float(i), forward_return=float(i) * 0.01)
        for i in range(MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE)
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson == pytest.approx(1.0)
    assert result.spearman == pytest.approx(1.0)
    assert result.sample_size == MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE
    assert result.approx_significant is True


def test_correlate_reports_perfect_negative_correlation():
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=float(i), forward_return=-float(i) * 0.01)
        for i in range(MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE)
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson == pytest.approx(-1.0)
    assert result.approx_significant is True


def test_correlate_never_flags_a_tiny_sample_as_significant_even_with_perfect_correlation():
    """Self-review finding: a technically-perfect |r|=1.0 from a handful
    of points is exactly the spurious-significance trap
    MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE exists to prevent -- this must
    hold for every domain, not just the ones that raise
    InsufficientSnapshotHistory below the same threshold."""
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=float(i), forward_return=float(i) * 0.01)
        for i in range(5)
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson == pytest.approx(1.0)
    assert result.sample_size == 5
    assert result.approx_significant is False


def test_correlate_with_fewer_than_three_observations_returns_none_not_a_crash():
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, 1), signal_value=1.0, forward_return=0.1),
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson is None
    assert result.spearman is None
    assert result.approx_significant is False
    assert result.sample_size == 1


def test_correlate_reports_no_significance_for_pure_noise():
    """A signal genuinely uncorrelated with the outcome must not be
    reported as significant just because some small |r| came out nonzero."""
    signal_values = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    forward_returns = [0.01, 0.01, -0.01, -0.01, 0.01, -0.01, -0.01, 0.01]
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=s, forward_return=r)
        for i, (s, r) in enumerate(zip(signal_values, forward_returns))
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson is not None
    assert abs(result.pearson) < 0.5
    assert result.approx_significant is False


# --- collect_technical_summary_observations: pairing + skip logic ----------


class _FakeSummary:
    def __init__(self, overall_score):
        self.overall_score = overall_score


class _FakeSupplemental:
    """Stands in for SupplementalResearchService -- isolates this module's
    own pairing/skip logic from the real indicator computation."""

    def __init__(self, engine=None):
        pass

    def get_technical_summary_as_of(self, ticker, as_of):
        return _FakeSummary(_SCORES_BY_TICKER_DATE[ticker].get(as_of))


_SCORES_BY_TICKER_DATE: dict[str, dict[date, float | None]] = {}


def test_collect_technical_summary_observations_pairs_correct_forward_return(engine, monkeypatch):
    closes = [100.0, 101.0, 102.0, 110.0, 104.0, 105.0, 106.0, 90.0, 108.0, 109.0]
    _seed_prices(engine, "NVDA", closes)
    monkeypatch.setattr(
        "alpha_lab.analytics.signal_predictive_value.SupplementalResearchService", _FakeSupplemental
    )
    _SCORES_BY_TICKER_DATE.clear()
    _SCORES_BY_TICKER_DATE["NVDA"] = {
        date(2026, 1, 1): 0.5,   # index 0 -> forward_days=3 -> index 3 (110.0)
        date(2026, 1, 3): -0.5,  # index 2 -> index 5 (105.0)
    }

    observations = collect_technical_summary_observations(
        engine, ["NVDA"], forward_days=3, sample_interval_days=2
    )

    assert len(observations) == 2
    first, second = observations
    assert first.ticker == "NVDA"
    assert first.as_of == date(2026, 1, 1)
    assert first.signal_value == 0.5
    assert first.forward_return == pytest.approx(110.0 / 100.0 - 1)
    assert second.as_of == date(2026, 1, 3)
    assert second.signal_value == -0.5
    assert second.forward_return == pytest.approx(105.0 / 102.0 - 1)


def test_collect_technical_summary_observations_skips_review_samples(engine, monkeypatch):
    closes = [100.0] * 10
    _seed_prices(engine, "NVDA", closes)
    monkeypatch.setattr(
        "alpha_lab.analytics.signal_predictive_value.SupplementalResearchService", _FakeSupplemental
    )
    _SCORES_BY_TICKER_DATE.clear()
    _SCORES_BY_TICKER_DATE["NVDA"] = {
        date(2026, 1, 1): None,  # REVIEW -- must be skipped
        date(2026, 1, 3): 0.2,
    }

    observations = collect_technical_summary_observations(
        engine, ["NVDA"], forward_days=3, sample_interval_days=2
    )

    assert len(observations) == 1
    assert observations[0].as_of == date(2026, 1, 3)


def test_collect_technical_summary_observations_skips_tickers_with_short_history(engine, monkeypatch):
    _seed_prices(engine, "NVDA", [100.0, 101.0])  # shorter than forward_days
    monkeypatch.setattr(
        "alpha_lab.analytics.signal_predictive_value.SupplementalResearchService", _FakeSupplemental
    )
    _SCORES_BY_TICKER_DATE.clear()
    _SCORES_BY_TICKER_DATE["NVDA"] = {}

    observations = collect_technical_summary_observations(
        engine, ["NVDA"], forward_days=20, sample_interval_days=20
    )
    assert observations == []


def test_collect_technical_summary_observations_handles_untracked_ticker(engine, monkeypatch):
    monkeypatch.setattr(
        "alpha_lab.analytics.signal_predictive_value.SupplementalResearchService", _FakeSupplemental
    )
    _SCORES_BY_TICKER_DATE.clear()
    _SCORES_BY_TICKER_DATE["NOPE"] = {}
    assert collect_technical_summary_observations(engine, ["NOPE"], forward_days=5, sample_interval_days=5) == []


# --- Analyst Consensus / AI Research Rating: gated below the threshold -----


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
        rating_version="test-v1", configuration_hash="test-config",
        evaluation_date=evaluation_date,
    )


def _analyst_consensus(rating_score: float) -> AnalystConsensus:
    return AnalystConsensus(ticker="NVDA", rating_score=rating_score, source="test", coverage=1.0, confidence=1.0)


def _seed_snapshot(engine, settings, ticker: str, created_at: datetime, rating_score: float) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
    Phase3Repository(engine).save_current_research([_record(ticker, created_at.date())])
    service = ResearchService(engine, settings)
    research = service.get_stock_research(ticker).model_copy(
        update={"analyst_consensus": _analyst_consensus(rating_score)}
    )
    summary = service.persist_snapshot(research)
    with Session(engine) as session:
        row = session.scalar(select(ResearchSnapshot).where(ResearchSnapshot.snapshot_id == summary.snapshot_id))
        row.created_at = created_at
        session.commit()


def test_collect_analyst_consensus_observations_raises_below_minimum(engine):
    settings = load_settings()
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(60)])
    for i in range(5):  # far fewer than MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE
        _seed_snapshot(engine, settings, "NVDA", datetime(2026, 1, 1) + timedelta(days=i), rating_score=1.0)

    with pytest.raises(InsufficientSnapshotHistory):
        collect_analyst_consensus_observations(engine, settings, ["NVDA"], forward_days=5)


def test_collect_analyst_consensus_observations_succeeds_once_enough_snapshots_exist(engine):
    settings = load_settings()
    n = MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE + 5
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(n + 10)])
    for i in range(n):
        _seed_snapshot(engine, settings, "NVDA", datetime(2026, 1, 1) + timedelta(days=i), rating_score=float(i % 3))

    observations = collect_analyst_consensus_observations(engine, settings, ["NVDA"], forward_days=5)
    assert len(observations) >= MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE
    assert all(o.ticker == "NVDA" for o in observations)
