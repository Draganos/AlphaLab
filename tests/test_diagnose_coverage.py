from datetime import date
import importlib.util
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.screener import LiveResearchRecord

SPEC = importlib.util.spec_from_file_location(
    "diagnose_coverage", Path(__file__).parents[1] / "scripts" / "diagnose_coverage.py"
)
diagnostic_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(diagnostic_module)
diagnose = diagnostic_module.diagnose


def test_diagnostic_distinguishes_missing_provider_data_from_zero(db_session):
    with Session(db_session.get_bind()) as session:
        session.add(Security(ticker="TEST", company_name="Test Corp"))
        session.add(
            Fundamental(
                ticker="TEST",
                period=date(2025, 12, 31),
                provider="SECCompanyFactsProvider",
                revenue=10.0,
                observation_hash="sec-only",
            )
        )
        session.commit()
    Phase3Repository(db_session.get_bind().engine).save_current_research([_record("TEST")])

    result = diagnose(db_session.get_bind().engine, ["TEST"])[0]

    assert result["stored_prices"] == {
        "count": 0,
        "minimum_date": None,
        "maximum_date": None,
        "providers": {},
    }
    assert result["stored_fundamental_providers"] == {"SECCompanyFactsProvider": 1}
    assert result["current_research"]["raw_metrics"]["valuation"]["pe"] is None
    assert result["current_research"]["raw_metrics"]["shareholder_return"][
        "dividend_yield"
    ] is None


def test_diagnostic_reports_stored_price_bounds_and_provider(db_session):
    with Session(db_session.get_bind()) as session:
        session.add(Security(ticker="TEST", company_name="Test Corp"))
        session.add_all(
            [
                Price(ticker="TEST", date=date(2025, 1, 2), close=10, provider="Provider"),
                Price(ticker="TEST", date=date(2025, 2, 3), close=11, provider="Provider"),
            ]
        )
        session.commit()
    Phase3Repository(db_session.get_bind().engine).save_current_research([_record("TEST", price=11.0)])

    result = diagnose(db_session.get_bind().engine, ["TEST"])[0]

    assert result["stored_prices"] == {
        "count": 2,
        "minimum_date": "2025-01-02",
        "maximum_date": "2025-02-03",
        "providers": {"Provider": 2},
    }
    assert result["current_research"]["latest_valid_price"] == 11.0


def _record(ticker: str, *, price: float | None = None) -> LiveResearchRecord:
    categories = {
        "earnings_growth": 0.0,
        "analyst_revisions": 0.0,
        "business_quality": 0.0,
        "valuation": 0.0,
        "momentum": 0.0,
        "financial_strength": 0.0,
        "ai_research": 0.0,
        "shareholder_return": 0.0,
    }
    return LiveResearchRecord(
        ticker=ticker,
        company="Test Corp",
        price=price,
        market_cap=None,
        country=None,
        exchange=None,
        sector=None,
        industry=None,
        asset_type=None,
        ethical_status="REVIEW",
        data_quality_status="missing price" if price is None else "valid",
        overall_score=None,
        category_scores={name: None for name in categories},
        category_coverage=categories,
        raw_metrics={},
        percentile_metrics={},
        overall_live_coverage=0.0,
        quantitative_coverage=0.0,
        ai_coverage=0.0,
        historical_coverage=0.0,
        confidence="insufficient data",
        provenance={},
        last_refreshed=None,
        configuration_hash="test",
        evaluation_date=date(2025, 2, 3),
    )
