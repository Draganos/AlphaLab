#!/usr/bin/env python
"""Load official SEC Companyfacts with append-only knowledge-time provenance."""

from pathlib import Path
import argparse
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.ingestion import SECIngestionService  # noqa: E402
from alpha_lab.providers import SECClient, SECCompanyFactsProvider  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Load official SEC filing facts")
    parser.add_argument("tickers", nargs="+", help="US ticker symbols")
    parser.add_argument("--user-agent", default=os.getenv("ALPHALAB_SEC_USER_AGENT"))
    args = parser.parse_args()
    if not args.user_agent:
        parser.error("Set --user-agent or ALPHALAB_SEC_USER_AGENT to 'App contact@email'")
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        provider = SECCompanyFactsProvider(SECClient(args.user_agent))
        mapping = provider.company_tickers()
        service = SECIngestionService(provider, engine)
        for ticker in (value.upper() for value in args.tickers):
            cik = mapping.get(ticker)
            if cik is None:
                print(f"{ticker}: SEC CIK unavailable; no data changed")
                continue
            try:
                raw, fundamentals = service.ingest(ticker, cik)
                print(f"{ticker}: added {raw} SEC facts and {fundamentals} filing snapshots")
            except Exception as error:
                print(f"{ticker}: SEC unavailable ({error}); prior data preserved")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
