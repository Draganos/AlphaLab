"""Deterministic, offline tests for SupplementalResearchService: current-row
persistence/read-back, provider-failure data preservation, and the AI
assessment's dependency on already-computed evidence. No network access."""

from datetime import date

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.research import CATEGORY_LABELS, CATEGORY_ORDER
from alpha_lab.research.ai_rating import AIDimensionValue
from alpha_lab.research.analyst_consensus import AnalystRating
from alpha_lab.research.model import CategoryResult, CategoryStatus, ConfidenceBreakdown, StockResearch
from alpha_lab.research.supplemental_service import SupplementalResearchService
from alpha_lab.research.technical import TechnicalRating


class _FakeAnalystProvider(MarketDataProvider):
    provider_name = "FakeProvider"

    def __init__(self, raw=None, raises: ProviderError | None = None):
        self._raw = raw
        self._raises = raises

    def get_company_info(self, ticker):
        return {}

    def get_price_history(self, ticker, start, end):
        return pd.DataFrame()

    def get_financials(self, ticker):
        return pd.DataFrame()

    def get_analyst_consensus(self, ticker):
        if self._raises is not None:
            raise self._raises
        return self._raw


def _seed_security_with_prices(engine, ticker="NVDA", days=300):
    with Session(engine) as session:
        session.add(Security(ticker=ticker))
        session.commit()
        for i in range(days):
            session.add(
                Price(
                    ticker=ticker,
                    date=date(2025, 1, 1) + pd.Timedelta(days=i),
                    close=100 + i * 0.5,
                    high=101 + i * 0.5,
                    low=99 + i * 0.5,
                )
            )
        session.commit()


def _stub_research(overall_coverage=0.5) -> StockResearch:
    categories = {
        name: CategoryResult(
            name=name, label=CATEGORY_LABELS[name],
            score=75.0 if name == "business_quality" else None,
            coverage=1.0 if name == "business_quality" else 0.0,
            status=CategoryStatus.AVAILABLE if name == "business_quality" else CategoryStatus.UNAVAILABLE,
            metrics=[], evidence=[], unavailable_metrics=[], sources=[],
        )
        for name in CATEGORY_ORDER
    }
    return StockResearch(
        ticker="NVDA", company_name="NVIDIA", sector=None, industry=None, security_type=None,
        categories=categories, overall_score=75.0, overall_coverage=overall_coverage,
        confidence=5.0, confidence_label="Moderate confidence", score_interpretation="Positive",
        confidence_breakdown=ConfidenceBreakdown(
            overall_coverage=overall_coverage, category_breadth=0.1, freshness=1.0,
            source_quality=1.0, data_quality_penalty_applied=False,
        ),
        strengths=[], weaknesses=[], risks=[], catalysts=[], sources=[],
        data_quality_status="valid", rating_version="v1", configuration_hash="cfg",
        evaluation_date=date.today(), generated_at=date.today(),
    )


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _raw_consensus(ticker="NVDA"):
    return {
        "ticker": ticker, "as_of": date.today(), "strong_buy": 18, "buy": 12, "hold": 5,
        "sell": 1, "strong_sell": 0, "target_current": 100.0, "target_low": 80.0,
        "target_mean": 130.0, "target_median": 125.0, "target_high": 160.0, "source": "FakeProvider",
    }


def test_analyst_consensus_round_trips_through_persistence(engine):
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    written = service.refresh_analyst_consensus("NVDA", _FakeAnalystProvider(_raw_consensus()))
    assert written.rating == AnalystRating.BUY
    read_back = service.get_analyst_consensus("NVDA")
    assert read_back is not None
    assert read_back.rating == AnalystRating.BUY
    assert read_back.total_analysts == 36


def test_technical_summary_round_trips_through_persistence(engine):
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    written = service.refresh_technical_summary("NVDA")
    assert written.coverage == 1.0
    read_back = service.get_technical_summary("NVDA")
    assert read_back is not None
    assert read_back.overall_rating == written.overall_rating


def test_missing_ticker_reads_return_none_not_a_default_object(engine):
    service = SupplementalResearchService(engine)
    assert service.get_analyst_consensus("NOPE") is None
    assert service.get_technical_summary("NOPE") is None
    assert service.get_ai_research_assessment("NOPE") is None


def test_failed_analyst_refresh_does_not_erase_previously_computed_consensus(engine):
    """The same invariant PR #16 established for ingestion: a provider
    failure must never erase previously valid data."""
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    service.refresh_analyst_consensus("NVDA", _FakeAnalystProvider(_raw_consensus()))
    before = service.get_analyst_consensus("NVDA")
    assert before is not None

    failing_provider = _FakeAnalystProvider(
        raises=ProviderError(ProviderErrorKind.RATE_LIMITED, "FakeProvider", "rate limited")
    )
    with pytest.raises(ProviderError):
        service.refresh_analyst_consensus("NVDA", failing_provider)

    after = service.get_analyst_consensus("NVDA")
    assert after is not None
    assert after.rating == before.rating
    assert after.total_analysts == before.total_analysts


def test_technical_summary_refresh_never_needs_a_provider_and_always_succeeds(engine):
    """No Security/Price rows at all -- must not raise, must yield an
    honest zero-coverage REVIEW summary."""
    with Session(engine) as session:
        session.add(Security(ticker="EMPTY"))
        session.commit()
    service = SupplementalResearchService(engine)
    summary = service.refresh_technical_summary("EMPTY")
    assert summary.coverage == 0.0
    assert summary.overall_rating == TechnicalRating.REVIEW


def test_ai_assessment_uses_the_explicitly_passed_analyst_and_technical_evidence(engine):
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    analyst = service.refresh_analyst_consensus("NVDA", _FakeAnalystProvider(_raw_consensus()))
    technical = service.refresh_technical_summary("NVDA")
    research = _stub_research()

    assessment = service.refresh_ai_research_assessment(
        "NVDA", research, analyst_consensus=analyst, technical_summary=technical
    )
    assert assessment.score is not None
    # business_outlook is derived from business_quality, which is AVAILABLE here.
    assert assessment.dimensions["business_outlook"].value != AIDimensionValue.REVIEW


def test_ai_assessment_round_trips_through_persistence(engine):
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    research = _stub_research()
    written = service.refresh_ai_research_assessment("NVDA", research)
    read_back = service.get_ai_research_assessment("NVDA")
    assert read_back is not None
    assert read_back.rating == written.rating
    assert read_back.score == written.score


def test_ai_assessment_never_touches_the_fundamental_score(engine):
    """Refreshing AI Research Rating must not mutate the StockResearch it
    was given -- the fundamental score is computed and stored entirely
    separately."""
    _seed_security_with_prices(engine)
    service = SupplementalResearchService(engine)
    research = _stub_research()
    original_score = research.overall_score
    original_categories = {name: cat.score for name, cat in research.categories.items()}

    service.refresh_ai_research_assessment("NVDA", research)

    assert research.overall_score == original_score
    assert {name: cat.score for name, cat in research.categories.items()} == original_categories
