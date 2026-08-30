from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import (
    CurrentResearchBuild,
    CurrentResearchSnapshot,
    Security,
)
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.screener import LiveResearchRecord


def _record(ticker: str, **updates) -> LiveResearchRecord:
    values = {
        "ticker": ticker,
        "company": ticker,
        "price": 100.0,
        "market_cap": 1_000.0,
        "country": "US",
        "exchange": "NASDAQ",
        "sector": "Technology",
        "industry": "Software",
        "asset_type": "equity",
        "themes": [],
        "ethical_status": "PASS",
        "data_quality_status": "valid",
        "overall_score": 75.0,
        "overall_rank": 1,
        "category_scores": {},
        "category_coverage": {},
        "raw_metrics": {},
        "percentile_metrics": {},
        "overall_live_coverage": 0.75,
        "quantitative_coverage": 0.75,
        "ai_coverage": 0.0,
        "historical_coverage": 0.0,
        "confidence": "Strong",
        "provenance": {},
        "last_refreshed": None,
        "rating_version": "phase3-live-v2",
        "configuration_hash": "configuration-a",
        "evaluation_date": date(2026, 8, 30),
    }
    values.update(updates)
    return LiveResearchRecord.model_validate(values)


@pytest.fixture
def current_research_repository(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'current-research.db'}")
    create_schema(engine)
    repository = Phase3Repository(engine)
    try:
        yield repository, engine
    finally:
        engine.dispose()


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as session:
        return (
            session.scalar(select(func.count()).select_from(CurrentResearchBuild)),
            session.scalar(select(func.count()).select_from(CurrentResearchSnapshot)),
        )


def test_current_research_build_rejects_mixed_configuration_hashes(
    current_research_repository,
):
    repository, engine = current_research_repository
    with pytest.raises(ValueError, match="mixed configuration hashes"):
        repository.save_current_research(
            [_record("A"), _record("B", configuration_hash="configuration-b")]
        )
    assert _counts(engine) == (0, 0)


def test_current_research_build_rejects_mixed_rating_versions(
    current_research_repository,
):
    repository, engine = current_research_repository
    with pytest.raises(ValueError, match="mixed rating versions"):
        repository.save_current_research(
            [_record("A"), _record("B", rating_version="phase3-live-v3")]
        )
    assert _counts(engine) == (0, 0)


def test_current_research_build_rejects_mixed_evaluation_dates(
    current_research_repository,
):
    repository, engine = current_research_repository
    with pytest.raises(ValueError, match="mixed evaluation dates"):
        repository.save_current_research(
            [_record("A"), _record("B", evaluation_date=date(2026, 8, 31))]
        )
    assert _counts(engine) == (0, 0)


def test_current_research_build_rejects_duplicate_tickers(
    current_research_repository,
):
    repository, engine = current_research_repository
    with pytest.raises(ValueError, match="tickers must be unique"):
        repository.save_current_research([_record("A"), _record("a")])
    assert _counts(engine) == (0, 0)


def test_failed_current_research_validation_writes_nothing(
    current_research_repository,
):
    repository, engine = current_research_repository
    with pytest.raises(ValueError, match="tickers must be non-empty"):
        repository.save_current_research([_record("   ")])
    with pytest.raises(TypeError, match="LiveResearchRecord"):
        repository.save_current_research([object()])  # type: ignore[list-item]
    assert _counts(engine) == (0, 0)


def test_homogeneous_current_research_batch_persists_normally(
    current_research_repository,
):
    repository, engine = current_research_repository
    with Session(engine) as session:
        session.add_all([Security(ticker="A"), Security(ticker="B")])
        session.commit()

    build = repository.save_current_research([_record("A"), _record("B")])

    assert build is not None
    assert build.evaluation_date == date(2026, 8, 30)
    assert build.score_version == "phase3-live-v2"
    assert build.config_hash == "configuration-a"
    assert build.security_count == 2
    assert _counts(engine) == (1, 2)
