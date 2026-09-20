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
from alpha_lab.database.models import CompanyDocument, Price, ResearchSnapshot, Security
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
    _forward_return_from,
    _price_series,
    collect_analyst_consensus_observations,
    collect_rule_based_ai_research_observations,
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


def test_correlate_reports_perfect_negative_correlation():
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=float(i), forward_return=-float(i) * 0.01)
        for i in range(MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE)
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson == pytest.approx(-1.0)


def test_correlate_reports_the_true_sample_size_even_for_a_tiny_sample():
    """CorrelationResult never claims a "significant" verdict (see this
    module's own docstring for why -- repeated/clustered snapshots for the
    same ticker are not independent observations, so a naive |r| >
    2/sqrt(n) threshold would overstate confidence). A technically-perfect
    |r|=1.0 from a handful of points is reported plainly, with its real
    sample_size, for the reader to judge -- never silently upgraded or
    downgraded."""
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=float(i), forward_return=float(i) * 0.01)
        for i in range(5)
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson == pytest.approx(1.0)
    assert result.sample_size == 5
    assert not hasattr(result, "approx_significant")


def test_correlate_with_fewer_than_three_observations_returns_none_not_a_crash():
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, 1), signal_value=1.0, forward_return=0.1),
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson is None
    assert result.spearman is None
    assert result.sample_size == 1


def test_correlate_reports_a_small_pearson_for_pure_noise():
    """A signal genuinely uncorrelated with the outcome should come out
    with a small |r| -- this is a math sanity check, not a significance
    claim (this module never computes one; see its own docstring)."""
    signal_values = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    forward_returns = [0.01, 0.01, -0.01, -0.01, 0.01, -0.01, -0.01, 0.01]
    observations = [
        SignalObservation(ticker="NVDA", as_of=date(2026, 1, i + 1), signal_value=s, forward_return=r)
        for i, (s, r) in enumerate(zip(signal_values, forward_returns))
    ]
    result = _correlate(observations, forward_days=20, signal_name="test")
    assert result.pearson is not None
    assert abs(result.pearson) < 0.5


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


def test_collect_analyst_consensus_observations_never_anchors_on_the_same_days_close(engine):
    """Regression test for a real PIT concern: entry.created_at is a real,
    uncontrolled intraday timestamp (unlike Technical Summary's own as_of,
    a deliberate end-of-day reconstruction boundary) -- a snapshot recorded
    mid-session must never be paired with that same day's own closing
    price, which plainly was not yet known at that moment. The entry price
    must be the first trading day's close strictly after the snapshot."""
    settings = load_settings()
    n = MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE + 5
    closes = [100.0 + i for i in range(n + 10)]
    _seed_prices(engine, "NVDA", closes)
    for i in range(n):
        # Recorded mid-session (11:00), not at a day boundary.
        _seed_snapshot(
            engine, settings, "NVDA",
            datetime(2026, 1, 1, 11, 0) + timedelta(days=i), rating_score=float(i % 3),
        )

    observations = collect_analyst_consensus_observations(engine, settings, ["NVDA"], forward_days=5)
    first_day_observation = next(o for o in observations if o.as_of == date(2026, 1, 1))

    same_day_return = closes[5] / closes[0] - 1
    next_day_return = closes[1 + 5] / closes[1] - 1
    assert first_day_observation.forward_return == pytest.approx(next_day_return)
    assert first_day_observation.forward_return != pytest.approx(same_day_return)


# --- _forward_return_from: the shared PIT-safe forward-return lookup -------


def test_forward_return_from_skips_when_as_of_predates_all_stored_price_history(engine):
    """Regression test for a real bug found while building the rule-based
    AI research calibration study: NVDA's own SEC filing history starts
    2020-08-19, but its stored Price history only starts 2021-09-15 --
    searchsorted's side="right" silently resolves any as_of before the
    whole series to position 0, which used to get paired with that single
    2021-09-15 price as if it were "the next trading day after" a filing
    from over a year earlier. Must be skipped, not silently misattributed."""
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(30)], start=date(2021, 9, 15))
    prices = _price_series(engine, "NVDA")
    assert _forward_return_from(prices, date(2020, 8, 19), forward_days=5) is None


def test_forward_return_from_computes_correctly_for_a_normal_gap(engine):
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(30)], start=date(2026, 1, 1))
    prices = _price_series(engine, "NVDA")
    # as_of is the same day as the first stored price -- the resolved
    # position is the very next day, a one-day gap, well within bounds.
    forward_return = _forward_return_from(prices, date(2026, 1, 1), forward_days=5)
    assert forward_return == pytest.approx(prices.iloc[6] / prices.iloc[1] - 1)


def test_forward_return_from_none_when_not_enough_future_history(engine):
    _seed_prices(engine, "NVDA", [100.0, 101.0, 102.0], start=date(2026, 1, 1))
    prices = _price_series(engine, "NVDA")
    assert _forward_return_from(prices, date(2026, 1, 1), forward_days=20) is None


# --- collect_rule_based_ai_research_observations: real filing-date replay --


def _seed_document(engine, ticker: str, document_date: date, text: str, *, doc_id: int | None = None) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
        document = CompanyDocument(
            ticker=ticker, document_date=document_date, document_type="10-K",
            title=f"{ticker} 10-K filed {document_date.isoformat()}", text=text,
            source="https://example.test", content_hash=f"{ticker}-{document_date.isoformat()}-{doc_id}",
        )
        session.add(document)
        session.commit()


def test_collect_rule_based_ai_research_observations_pairs_cumulative_documents_with_forward_return(engine):
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(60)], start=date(2026, 1, 1))
    _seed_document(engine, "NVDA", date(2026, 1, 5), "Strong demand for our products this quarter.", doc_id=1)
    _seed_document(engine, "NVDA", date(2026, 2, 10), "Margin expansion continued in the period.", doc_id=2)

    observations = collect_rule_based_ai_research_observations(engine, ["NVDA"], forward_days=5)

    assert [o.as_of for o in observations] == [date(2026, 1, 5), date(2026, 2, 10)]
    # The second observation's document set is cumulative (both filings),
    # so its ai_rating differs from the first (one real filing only).
    assert observations[0].signal_value != observations[1].signal_value
    assert all(isinstance(o.forward_return, float) for o in observations)


def test_collect_rule_based_ai_research_observations_windows_out_old_filings(engine):
    """Regression test for the real score-saturation finding this fix
    addresses: a filing from more than DOCUMENT_ANALYSIS_WINDOW_DAYS
    before as_of must not be fed into the reconstruction just because it
    predates as_of -- only the trailing window, matching what
    AIResearchService.ensure_all actually does in production."""
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(760)], start=date(2024, 1, 1))
    _seed_document(engine, "NVDA", date(2024, 1, 5), "Strong demand for our products this quarter.", doc_id=1)
    _seed_document(engine, "NVDA", date(2026, 1, 5), "Margin decline continued in the period.", doc_id=2)

    observations = collect_rule_based_ai_research_observations(engine, ["NVDA"], forward_days=5)

    as_of_2026 = next(o for o in observations if o.as_of == date(2026, 1, 5))
    # Only the second (2026) filing is inside the window at that as_of --
    # its own phrase ("margin decline") should dominate, not a mix with
    # the 2024 filing's "strong demand".
    from alpha_lab.ai.rule_based import RuleBasedFinancialResearchProvider
    solo_result = RuleBasedFinancialResearchProvider().analyze(
        "NVDA", [{"id": 2, "text": "Margin decline continued in the period.",
                   "title": "t", "source": "s", "document_date": "2026-01-05"}],
    )
    assert as_of_2026.signal_value == solo_result.ai_rating


def test_collect_rule_based_ai_research_observations_skips_tickers_with_no_documents(engine):
    _seed_prices(engine, "NVDA", [100.0 + i for i in range(30)], start=date(2026, 1, 1))
    assert collect_rule_based_ai_research_observations(engine, ["NVDA"], forward_days=5) == []


def test_collect_rule_based_ai_research_observations_skips_tickers_with_no_prices(engine):
    _seed_document(engine, "NVDA", date(2026, 1, 5), "Strong demand.", doc_id=1)
    assert collect_rule_based_ai_research_observations(engine, ["NVDA"], forward_days=5) == []
