from datetime import date, timedelta

from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Estimate, Fundamental, Price, Security
from alpha_lab.screener import MarketScreenerService
from alpha_lab.screener.service import CATEGORY_PROVENANCE


def test_category_provenance_mapping_is_semantically_correct():
    assert CATEGORY_PROVENANCE["momentum"] == "price"
    assert CATEGORY_PROVENANCE["analyst_revisions"] == "estimate"
    assert CATEGORY_PROVENANCE["valuation"] == "fundamental"
    assert CATEGORY_PROVENANCE["business_quality"] == "fundamental"
    assert CATEGORY_PROVENANCE["ai_research"] == "ai"


def test_live_screener_filters_future_evidence_and_excludes_stale_reference(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    today = date.today()
    try:
        create_schema(engine)
        with Session(engine) as session:
            for ticker in ("VALID", "STALE"):
                session.add(
                    Security(
                        ticker=ticker,
                        company_name=ticker,
                        sector="Technology",
                        industry="Software",
                        business_description="Enterprise software operating business with recurring subscriptions",
                        metadata_source="fixture",
                        market_cap=1_000_000,
                    )
                )
            for offset in range(30):
                day = today - timedelta(days=29 - offset)
                session.add(
                    Price(
                        ticker="VALID",
                        date=day,
                        close=100 + offset,
                        adjusted_close=100 + offset,
                        volume=1_000_000,
                        provider="fixture",
                        source="fixture-prices",
                    )
                )
                session.add(
                    Price(
                        ticker="STALE",
                        date=day - timedelta(days=30),
                        close=10 + offset,
                        adjusted_close=10 + offset,
                        volume=1_000_000,
                        provider="fixture",
                        source="fixture-prices",
                    )
                )
            session.add(
                Fundamental(
                    ticker="VALID",
                    period=today - timedelta(days=365),
                    publication_date=today - timedelta(days=30),
                    revenue=100,
                    ebitda=20,
                    net_income=10,
                    eps=1,
                    free_cash_flow=8,
                    total_debt=10,
                    cash=5,
                    total_equity=50,
                    shares_outstanding=10,
                    provider="fixture",
                    source="fixture-fundamentals",
                    observation_hash="known",
                )
            )
            session.add(
                Fundamental(
                    ticker="VALID",
                    period=today,
                    publication_date=today + timedelta(days=1),
                    revenue=99999,
                    ebitda=99999,
                    net_income=99999,
                    eps=99999,
                    free_cash_flow=99999,
                    total_debt=0,
                    cash=99999,
                    total_equity=99999,
                    shares_outstanding=10,
                    provider="future",
                    source="future-fundamentals",
                    observation_hash="future",
                )
            )
            session.add(
                Estimate(
                    ticker="VALID",
                    observation_date=today,
                    fiscal_period=today + timedelta(days=365),
                    consensus_eps=2,
                    provider="fixture",
                    source="fixture-estimates",
                    observation_hash="estimate-known",
                )
            )
            session.add(
                Estimate(
                    ticker="VALID",
                    observation_date=today + timedelta(days=1),
                    fiscal_period=today + timedelta(days=365),
                    consensus_eps=999,
                    provider="future",
                    source="future-estimates",
                    observation_hash="estimate-future",
                )
            )
            session.commit()
        settings = load_settings().model_copy(
            update={"database_url": f"sqlite:///{tmp_path / 'live.db'}"}
        )
        records = {
            item.ticker: item
            for item in MarketScreenerService(engine, settings).build_live_records()
        }
        assert records["VALID"].raw_metrics["current_consensus_eps"] == 2
        assert records["VALID"].raw_metrics["market_cap"] == 1290
        assert records["VALID"].data_quality_status == "valid"
        assert records["VALID"].provenance["price"]["source"] == "fixture-prices"
        assert (
            records["VALID"].provenance["fundamental"]["source"]
            == "fixture-fundamentals"
        )
        assert records["VALID"].provenance["estimate"]["source"] == "fixture-estimates"
        assert records["STALE"].data_quality_status == "stale price"
        assert records["STALE"].overall_rank is None
        assert records["STALE"].percentile_metrics["return_1m"] is not None
    finally:
        engine.dispose()


def test_excluded_company_is_scored_against_pass_reference_but_never_ranked(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine(f"sqlite:///{tmp_path / 'researchable-excluded.db'}")
    today = date.today()
    try:
        create_schema(engine)
        with Session(engine) as session:
            session.add(
                Security(
                    ticker="PASS",
                    sector="Technology",
                    industry="Software",
                    business_description="Enterprise software operating business with recurring subscriptions",
                    metadata_source="fixture",
                )
            )
            session.add(
                Security(
                    ticker="BANK",
                    sector="Financials",
                    industry="Regional Banks",
                    business_description="Bank holding company accepting deposits and originating loans",
                    metadata_source="fixture",
                )
            )
            for ticker, base_price in (("PASS", 20), ("BANK", 10)):
                for offset in range(30):
                    session.add(
                        Price(
                            ticker=ticker,
                            date=today - timedelta(days=29 - offset),
                            close=base_price + offset,
                            adjusted_close=base_price + offset,
                            volume=1_000_000,
                            provider="fixture",
                            source="fixture-prices",
                        )
                    )
                session.add(
                    Fundamental(
                        ticker=ticker,
                        period=today - timedelta(days=90),
                        publication_date=today - timedelta(days=30),
                        revenue=100,
                        ebitda=20,
                        net_income=10,
                        eps=2 if ticker == "PASS" else 1,
                        free_cash_flow=8,
                        total_debt=10,
                        cash=5,
                        total_equity=50,
                        shares_outstanding=10,
                        provider="fixture",
                        source="fixture-fundamentals",
                        observation_hash=f"{ticker}-fund",
                    )
                )
            session.commit()
        settings = load_settings().model_copy(
            update={
                "database_url": f"sqlite:///{tmp_path / 'researchable-excluded.db'}"
            }
        )
        records = {
            item.ticker: item
            for item in MarketScreenerService(engine, settings).build_live_records()
        }
        assert records["BANK"].ethical_status == "EXCLUDED"
        assert records["BANK"].category_scores["valuation"] is not None
        assert records["BANK"].overall_rank is None
        assert records["PASS"].overall_rank == 1
    finally:
        engine.dispose()
