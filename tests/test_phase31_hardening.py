from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import (
    CurrentResearchBuild,
    CurrentResearchSnapshot,
    AIResearchAnalysis,
    Fundamental,
    Price,
    SECCompanyFact,
    Security,
)
from alpha_lab.database.queries import latest_fundamentals_as_of
from alpha_lab.ingestion import SECIngestionService
from alpha_lab.portfolio import Candidate, construct_portfolio
from alpha_lab.providers.capabilities import (
    CAPABILITY_FIELDS,
    Capability,
    capability,
    preferred_provider,
    provider_capability_matrix,
)
from alpha_lab.providers.sec_edgar import (
    SECCompanyFactsProvider,
    parse_companyfacts,
    select_filing_metrics,
)
from alpha_lab.ratings import summarize_coverage
from alpha_lab.screener import LiveResearchRecord, MarketScreenerService
from alpha_lab.screener.service import (
    _category_evidence_coverage,
    _ai_is_attributable,
    _category_score,
    _live_data_quality_reason,
    _select_live_fundamental_values,
)
from alpha_lab.search import DeterministicQueryInterpreter, ScreenCriteria, ScreenRecord


def _fact(value, *, filed, accession, form="10-K", unit="USD", start="2023-01-01"):
    return {
        "start": start,
        "end": "2023-12-31",
        "val": value,
        "accn": accession,
        "fy": 2023,
        "fp": "FY",
        "form": form,
        "filed": filed,
    }


def _companyfacts(*rows, unit="USD", concept="Revenues"):
    return {
        "facts": {
            "us-gaap": {
                concept: {"units": {unit: list(rows)}}
            }
        }
    }


def _record(ticker="X", coverage=.75):
    categories = {
        "earnings_growth": .5,
        "analyst_revisions": 0.0,
        "business_quality": 1.0,
        "valuation": .8,
        "momentum": 1.0,
        "financial_strength": .8,
        "ai_research": 0.0,
        "shareholder_return": .5,
    }
    return LiveResearchRecord(
        ticker=ticker, company=ticker, price=100, market_cap=1_000,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="equity", themes=[], ethical_status="PASS",
        data_quality_status="valid", overall_score=75, overall_rank=1,
        category_scores={name: 75 if value else None for name, value in categories.items()},
        category_coverage=categories, raw_metrics={}, percentile_metrics={},
        overall_live_coverage=coverage, quantitative_coverage=.8, ai_coverage=0,
        historical_coverage=.5, confidence="Strong", provenance={},
        last_refreshed=None, configuration_hash="hash", evaluation_date=date.today(),
    )


def test_provider_capabilities_and_field_priority_fail_closed():
    assert capability("SECCompanyFactsProvider", "fundamentals.reported") == Capability.RELIABLE_POINT_IN_TIME
    assert capability("unknown", "unknown") == Capability.UNSUPPORTED
    assert preferred_provider(
        "fundamentals.reported", {"YFinanceProvider", "SECCompanyFactsProvider"}
    ) == "SECCompanyFactsProvider"
    assert all(
        set(fields) == set(CAPABILITY_FIELDS)
        for fields in provider_capability_matrix().values()
    )


def test_ai_coverage_requires_complete_attributable_input_identity():
    incomplete = AIResearchAnalysis(
        ticker="X", source_document_ids=[1], analyzed_document_ids=None,
        input_fingerprint=None, component_scores={}, key_positives=[], key_risks=[],
        evidence=[{"document_id": 1, "excerpt": "source"}], provider="fixture",
        model="fixture", prompt_version="v1", raw_output={}, ai_rating=50, confidence=1,
    )
    assert not _ai_is_attributable(incomplete)
    incomplete.analyzed_document_ids = [1]
    incomplete.input_fingerprint = "fingerprint"
    assert _ai_is_attributable(incomplete)


def test_sec_amendment_is_append_only_and_invisible_before_filed_date(tmp_path):
    payload = _companyfacts(
        _fact(100, filed="2024-02-15", accession="original"),
        _fact(120, filed="2024-06-15", accession="amended", form="10-K/A"),
    )

    class FixtureSEC(SECCompanyFactsProvider):
        def __init__(self):
            pass

        def get_facts(self, ticker, cik, *, as_of=None):
            return parse_companyfacts(
                payload, ticker, cik, source_url="https://data.sec.gov/fixture", as_of=as_of
            )

    engine = make_engine(f"sqlite:///{tmp_path / 'sec.db'}")
    try:
        create_schema(engine)
        raw, snapshots = SECIngestionService(FixtureSEC(), engine).ingest("SECX", "1")
        assert raw == snapshots == 2
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(SECCompanyFact)) == 2
            march = latest_fundamentals_as_of(session, "SECX", date(2024, 3, 1))
            july = latest_fundamentals_as_of(session, "SECX", date(2024, 7, 1))
            assert march[0].revenue == 100
            assert july[0].revenue == 120
            assert july[0].provenance_json["accession"] == "amended"
    finally:
        engine.dispose()


def test_current_only_yfinance_fundamental_has_no_historical_knowledge_time(db_session):
    db_session.add(Security(ticker="CURRENT"))
    db_session.flush()
    db_session.add(Fundamental(
        ticker="CURRENT", period=date(2020, 12, 31), publication_date=None,
        revenue=1_000, provider="YFinanceProvider", source="current-only",
        observation_hash="current-only",
    ))
    db_session.flush()
    assert latest_fundamentals_as_of(db_session, "CURRENT", date(2025, 1, 1)) == []


def test_sec_conflicting_units_and_unknown_concepts_remain_unavailable():
    wrong_unit = parse_companyfacts(
        _companyfacts(
            _fact(100, filed="2024-02-15", accession="a"),
            unit="EUR",
        ),
        "X", "1", source_url="fixture",
    )
    unknown = parse_companyfacts(
        _companyfacts(
            _fact(100, filed="2024-02-15", accession="a"),
            concept="IssuerSpecificAdjustedEBITDA",
        ),
        "X", "1", source_url="fixture",
    )
    assert wrong_unit == []
    assert unknown == []
    assert select_filing_metrics(wrong_unit + unknown) == []


def test_sec_conflicting_duplicate_fact_values_are_not_silently_selected():
    payload = _companyfacts(
        _fact(100, filed="2024-02-15", accession="same"),
        _fact(200, filed="2024-02-15", accession="same"),
    )
    assert parse_companyfacts(payload, "X", "1", source_url="fixture") == []


def test_live_field_priority_is_explicit_and_growth_stays_provider_consistent():
    today = date.today()
    rows = [
        Fundamental(
            id=1, ticker="X", period=today, publication_date=today,
            revenue=100, ebitda=None, provider="SECCompanyFactsProvider",
            observation_hash="sec",
        ),
        Fundamental(
            id=2, ticker="X", period=today, publication_date=None,
            revenue=999, ebitda=40, provider="YFinanceProvider", observation_hash="yf",
        ),
        Fundamental(
            id=3, ticker="X", period=today - timedelta(days=365),
            publication_date=today - timedelta(days=300), revenue=80,
            provider="SECCompanyFactsProvider", observation_hash="sec-old",
        ),
    ]
    current, prior, provenance = _select_live_fundamental_values(list(reversed(rows)))
    assert current["revenue"] == 100
    assert prior["revenue"] == 80
    assert current["ebitda"] == 40
    assert provenance["revenue"]["provider"] == "SECCompanyFactsProvider"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), 0, -1])
def test_invalid_prices_do_not_count_as_live_evidence(invalid):
    today = date.today()
    prices = [Price(ticker="X", date=today, close=invalid, adjusted_close=None, volume=1e6)]
    assert _live_data_quality_reason(prices, today, load_settings()) == "missing price"


def test_review_and_unknown_candidates_fail_closed_but_phase2_default_is_unchanged():
    candidates = [Candidate("REVIEW", 90, 1, ethical_status="REVIEW"), Candidate("UNKNOWN", 90, 1)]
    filtered = construct_portfolio(
        candidates, method="equal", min_score=0, minimum_coverage=0, min_positions=1,
        max_positions=2, max_position=1, max_sector=None, ethical_filter_enabled=True,
    )
    assert filtered.weights == {}
    unfiltered = construct_portfolio(
        candidates, method="equal", min_score=0, minimum_coverage=0, min_positions=1,
        max_positions=2, max_position=1, max_sector=None,
    )
    assert set(unfiltered.weights) == {"REVIEW", "UNKNOWN"}


def test_query_bounds_and_sector_boundaries_are_revalidated():
    interpreter = DeterministicQueryInterpreter()
    assert interpreter.interpret("financially strong technology companies").sectors == ["Technology"]
    assert interpreter.interpret("financial companies").sectors == ["Financials"]
    assert interpreter.interpret("score above -1").minimum_overall_score is None
    assert interpreter.interpret("coverage above 101%").minimum_coverage is None
    with pytest.raises(ValidationError):
        ScreenCriteria(minimum_overall_score=-1)
    with pytest.raises(ValidationError):
        ScreenRecord(ticker="NAN", overall_score=float("nan"))


def test_category_minimum_evidence_and_25_security_coverage_report():
    thin = pd.Series({"gross_margin": 50.0})
    assert _category_score(thin, "business_quality") is None
    coverage = _category_evidence_coverage(thin, ai_available=False)
    assert coverage["business_quality"] == pytest.approx(1 / 7)
    records = [_record(f"C{index:02}") for index in range(25)]
    report = summarize_coverage(records)
    assert report.count == 25
    assert report.median == report.p25 == report.p75 == .75
    assert report.minimum == report.maximum == .75


def test_missing_shareholder_component_is_not_assumed_zero():
    from alpha_lab.ratings import calculate_quality_factors

    factors = calculate_quality_factors(
        {"market_cap": 1_000, "dividends_paid": -10, "share_repurchases": None}
    )
    assert factors["dividend_yield"] == .01
    assert factors["buyback_yield"] is None
    assert factors["total_shareholder_yield"] is None


def test_current_snapshot_read_is_read_only_and_historical_service_does_not_consume_it(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    try:
        create_schema(engine)
        with Session(engine) as session:
            session.add(Security(ticker="X"))
            session.commit()
        from alpha_lab.phase3 import Phase3Repository
        Phase3Repository(engine).save_current_research([_record()])
        service = MarketScreenerService(engine, load_settings())
        monkeypatch.setattr(service, "build_live_records", lambda: pytest.fail("read rebuilt research"))
        assert service.read_current_research()[0].ticker == "X"
        with Session(engine) as session:
            before = session.scalar(select(func.count()).select_from(CurrentResearchBuild))
            snapshots = session.scalar(select(func.count()).select_from(CurrentResearchSnapshot))
        assert service.read_current_research()[0].ticker == "X"
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CurrentResearchBuild)) == before
            assert session.scalar(select(func.count()).select_from(CurrentResearchSnapshot)) == snapshots
        from alpha_lab.strategy import HistoricalScoringService
        historical = HistoricalScoringService(engine, load_settings()).score_universe_as_of(date.today())
        assert historical[0].exclusion_reason == "missing price"
    finally:
        engine.dispose()


def test_streamlit_render_paths_read_snapshots_and_home_limits_legacy_universe():
    root = Path(__file__).resolve().parents[1]
    market = (root / "app/dashboard/pages/3_Market_Screener.py").read_text()
    company = (root / "app/dashboard/pages/4_Company_Research.py").read_text()
    home = (root / "app/dashboard/main.py").read_text()
    research_service = (root / "alpha_lab/research/service.py").read_text()
    assert ".read_current_research()" in market and ".build_live_records()" not in market
    assert "interpret_query(" not in market
    # Company Research now reads through alpha_lab.research.ResearchService
    # rather than calling MarketScreenerService directly; the safety
    # guarantee moves with it — the page must only call the service's own
    # read-only accessor, and the service itself must only ever call the
    # cheap persisted-snapshot read, never the expensive whole-universe
    # rebuild.
    assert ".list_current_research()" in company and ".build_live_records()" not in company
    assert ".read_current_research()" in research_service
    assert ".build_live_records()" not in research_service
    assert 'tickers=settings.universe.get("us", [])' in home
    assert "ttl=900" in home
