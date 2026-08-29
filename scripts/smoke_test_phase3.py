"""Offline deterministic Phase 3 market-research smoke test."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.ai import DeterministicAIResearchProvider, analyze_documents  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import CompanyDocument, Fundamental, Price, Security  # noqa: E402
from alpha_lab.ethics import BusinessEvidence, evaluate_business, load_ethics_policy  # noqa: E402
from alpha_lab.phase3 import Phase3Repository  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402
from alpha_lab.search import ScreenRecord, apply_screen, interpret_query  # noqa: E402
from alpha_lab.themes import derive_themes  # noqa: E402


def main() -> None:
    companies = {
        "TECH": (
            "Evidence Technology",
            "Technology",
            "Semiconductors",
            "artificial intelligence semiconductor",
            ["payment_networks"],
        ),
        "AIR": (
            "Evidence Air",
            "Industrials",
            "Airlines",
            "passenger aviation airline",
            ["airlines"],
        ),
        "PAY": (
            "Evidence Payments",
            "Financials",
            "Payments",
            "payment network infrastructure",
            ["payment_processing"],
        ),
        "BANK": (
            "Evidence Bank",
            "Financials",
            "Banks",
            "conventional commercial bank",
            ["conventional_banking"],
        ),
        "ARMS": (
            "Evidence Arms",
            "Industrials",
            "Aerospace & Defense",
            "weapons manufacturing",
            ["weapons"],
        ),
    }
    with tempfile.TemporaryDirectory() as directory:
        engine = make_engine(f"sqlite:///{Path(directory) / 'phase3.db'}")
        try:
            create_schema(engine)
            start = date(2024, 1, 1)
            with Session(engine) as session:
                for ticker, (
                    name,
                    sector,
                    industry,
                    description,
                    _,
                ) in companies.items():
                    session.add(
                        Security(
                            ticker=ticker,
                            company_name=name,
                            exchange="NASDAQ",
                            country="US",
                            sector=sector,
                            industry=industry,
                            currency="USD",
                            asset_type="equity",
                            business_description=description,
                            market_cap=1_000_000_000,
                            metadata_provider="fixture",
                            metadata_source="phase3-smoke",
                        )
                    )
                    for offset in range(90):
                        day = start + timedelta(days=offset)
                        value = 50 + offset * (0.2 if ticker != "BANK" else -0.05)
                        session.add(
                            Price(
                                ticker=ticker,
                                date=day,
                                open=value,
                                close=value,
                                adjusted_close=value,
                                volume=1_000_000,
                                currency="USD",
                                provider="fixture",
                                source="phase3-smoke",
                            )
                        )
                    session.add(
                        Fundamental(
                            ticker=ticker,
                            period=date(2023, 12, 31),
                            publication_date=date(2024, 2, 1),
                            revenue=1_000,
                            gross_profit=500,
                            ebitda=300,
                            ebit=250,
                            net_income=200,
                            eps=2,
                            free_cash_flow=180,
                            total_debt=100,
                            cash=50,
                            total_equity=600,
                            total_assets=1_200,
                            shares_outstanding=10,
                            currency="USD",
                            provider="fixture",
                            source="phase3-smoke",
                            observation_hash=f"{ticker}-fundamental",
                        )
                    )
                    session.add(
                        CompanyDocument(
                            ticker=ticker,
                            document_date=date(2024, 3, 1),
                            document_type="earnings_release",
                            title="Fixture release",
                            text="Strong demand and raised guidance with margin expansion.",
                            source="phase3-smoke",
                            processed=False,
                        )
                    )
                session.commit()
            policy, repository = load_ethics_policy(), Phase3Repository(engine)
            with Session(engine) as session:
                documents = list(session.query(CompanyDocument))
            for ticker, (_, _, _, description, tags) in companies.items():
                decision = evaluate_business(
                    BusinessEvidence(
                        ticker=ticker,
                        primary_business=description,
                        business_tags=tags,
                        evidence=[{"source": "phase3-smoke", "text": description}],
                        source="phase3-smoke",
                        financial_warnings=["Debt is displayed, not a hard exclusion"],
                    ),
                    policy,
                    evaluated_at=datetime(2024, 3, 31, tzinfo=UTC),
                )
                repository.save_ethics(decision)
                repository.save_themes(
                    ticker, derive_themes(description, "phase3-smoke")
                )
                document = next(item for item in documents if item.ticker == ticker)
                analysis = analyze_documents(
                    DeterministicAIResearchProvider(),
                    ticker,
                    [{"id": document.id, "text": document.text}],
                )
                assert analysis is not None
                repository.save_ai(ticker, analysis)
            records = MarketScreenerService(engine, load_settings()).build_live_records(
                date(2024, 3, 30)
            )
            statuses = {item.ticker: item.ethical_status for item in records}
            assert statuses["BANK"] == statuses["ARMS"] == "EXCLUDED"
            assert statuses["AIR"] == statuses["PAY"] == "PASS"
            query = interpret_query(
                "Find Sharia-preferred payment infrastructure with score above 0"
            )
            results = apply_screen(
                [
                    ScreenRecord(
                        ticker=item.ticker,
                        company_name=item.company,
                        themes=item.themes,
                        ethical_status=item.ethical_status,
                        overall_score=item.overall_score,
                        growth_score=item.category_scores.get("earnings_growth"),
                        market_cap=item.market_cap,
                        coverage=item.overall_live_coverage,
                    )
                    for item in records
                ],
                query,
            )
            assert [item.ticker for item in results] == ["PAY"]
            for item in records:
                print(
                    f"{item.ticker}: status={item.ethical_status} rating={item.overall_score} coverage={item.overall_live_coverage:.0%}"
                )
            print("Phase 3 smoke test passed")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
