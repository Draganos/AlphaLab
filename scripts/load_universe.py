#!/usr/bin/env python
"""Load a broad credential-free US universe and optionally enrich live metadata."""

from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.ingestion import UniverseIngestionService  # noqa: E402
from alpha_lab.providers import NasdaqTraderUniverseProvider, YFinanceProvider  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Load broad market security metadata")
    parser.add_argument("--market", default="US", choices=["US"])
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--skip-enrichment", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        service = UniverseIngestionService(NasdaqTraderUniverseProvider(), engine)
        count = service.load(country=args.market, exchanges=("NASDAQ", "NYSE"))
        print(f"Loaded the full {count}-symbol active non-ETF NYSE/NASDAQ directory")
        if not args.skip_enrichment:
            tickers = service.research_tickers(limit=args.limit)
            provider = YFinanceProvider()
            enriched = []
            for ticker in tickers:
                try:
                    enriched.append(provider.get_company_info(ticker))
                except Exception as error:
                    print(f"Metadata unavailable for {ticker}: {error}")
            service.enrich(enriched)
            print(f"Enriched {len(enriched)} securities with available live metadata")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
