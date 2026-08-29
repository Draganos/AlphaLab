#!/usr/bin/env python
"""Populate genuine live price/fundamental evidence for the stored universe."""

from datetime import date, timedelta
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import Security  # noqa: E402
from alpha_lab.ingestion import IngestionService  # noqa: E402
from alpha_lab.providers import YFinanceProvider  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load live research evidence without fabricating history"
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--years", type=int, default=2)
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        with Session(engine) as session:
            tickers = list(
                session.scalars(
                    select(Security.ticker).order_by(Security.ticker).limit(args.limit)
                )
            )
        service = IngestionService(YFinanceProvider(), engine)
        completed = 0
        for ticker in tickers:
            try:
                service.ingest(
                    ticker,
                    date.today() - timedelta(days=365 * args.years),
                    date.today(),
                )
                completed += 1
            except Exception as error:
                print(f"Live data unavailable for {ticker}: {error}")
        print(
            f"Loaded genuine available live data for {completed}/{len(tickers)} securities"
        )
        print(
            "Expected categories where source data permits: growth, quality, valuation, momentum, financial strength (~70%)."
        )
        print(
            "Estimates, AI, shareholder return, and publication dates remain unavailable unless actually observed."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
