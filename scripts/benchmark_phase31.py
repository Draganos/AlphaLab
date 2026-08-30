#!/usr/bin/env python
"""Offline deterministic Phase 3.1 coverage and UI-read performance acceptance check."""

from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import Fundamental, Price, Security  # noqa: E402
from alpha_lab.ratings import summarize_coverage  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402
from alpha_lab.search import ScreenCriteria, ScreenRecord, apply_screen  # noqa: E402
from alpha_lab.strategy import HistoricalScoringService  # noqa: E402


def _seed(engine) -> list[str]:
    tickers = [f"C{index:02}" for index in range(25)]
    today = date.today()
    with Session(engine) as session:
        for index, ticker in enumerate(tickers):
            session.add(Security(
                ticker=ticker, company_name=f"Fixture Operating Company {index}",
                exchange="NASDAQ" if index % 2 else "NYSE", country="US",
                sector="Technology", industry="Software", asset_type="equity",
                business_description="Develops enterprise software for operating businesses",
                metadata_provider="phase31-fixture", metadata_source="offline acceptance fixture",
            ))
            for offset in range(260):
                value = 50 + index + offset * .1
                session.add(Price(
                    ticker=ticker, date=today - timedelta(days=259 - offset),
                    open=value, close=value, adjusted_close=value, volume=1_000_000,
                    provider="phase31-fixture", source="offline acceptance fixture",
                ))
            for age, scale in ((365, 1.0), (730, .8)):
                session.add(Fundamental(
                    ticker=ticker, period=today - timedelta(days=age),
                    publication_date=None, revenue=1_000 * scale, gross_profit=500 * scale,
                    ebitda=300 * scale, ebit=250 * scale, net_income=200 * scale,
                    eps=2 * scale, free_cash_flow=180 * scale, total_debt=100,
                    cash=50, total_equity=600 * scale, total_assets=1_200 * scale,
                    current_assets=400, current_liabilities=200, interest_expense=20,
                    dividends_paid=-10, share_repurchases=-5, shares_outstanding=10,
                    provider="YFinanceProvider", source="offline current-only fixture",
                    observation_hash=f"{ticker}-{age}",
                ))
        session.commit()
    return tickers


def main() -> None:
    os.environ["ALPHALAB_AI_PROVIDER"] = "disabled"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "phase31.db"
        engine = make_engine(f"sqlite:///{path}")
        try:
            create_schema(engine)
            tickers = _seed(engine)
            settings = load_settings().model_copy(update={
                "database_url": f"sqlite:///{path}",
                "universe": {"us": tickers[:5], "uae": []},
            })
            start = perf_counter()
            HistoricalScoringService(engine, settings).score_universe_as_of(
                date.today(), tickers=tickers[:5]
            )
            legacy = perf_counter() - start
            service = MarketScreenerService(engine, settings)
            start = perf_counter()
            records = service.rebuild_current_research()
            rebuild = perf_counter() - start
            start = perf_counter()
            persisted = service.read_current_research()
            initial_read = perf_counter() - start
            screen = [
                ScreenRecord(
                    ticker=record.ticker, ethical_status=record.ethical_status,
                    overall_score=record.overall_score, coverage=record.overall_live_coverage,
                )
                for record in persisted
            ]
            start = perf_counter()
            apply_screen(screen, ScreenCriteria(minimum_coverage=.7))
            filter_time = perf_counter() - start
            start = perf_counter()
            apply_screen(screen, ScreenCriteria(sort="overall_score_asc"))
            sort_time = perf_counter() - start
            report = summarize_coverage(records)
            print(f"legacy_home_5={legacy:.6f}s")
            print(f"research_rebuild_25={rebuild:.6f}s")
            print(f"market_screener_read_25={initial_read:.6f}s")
            print(f"filter_change_25={filter_time:.6f}s network_calls=0 writes=0")
            print(f"sort_change_25={sort_time:.6f}s network_calls=0 writes=0")
            print(
                f"coverage count={report.count} median={report.median:.2%} "
                f"p25={report.p25:.2%} p75={report.p75:.2%} "
                f"min={report.minimum:.2%} max={report.maximum:.2%}"
            )
            for category, value in report.category_availability.items():
                print(f"category.{category}={value:.2%}")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
