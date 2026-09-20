#!/usr/bin/env python
"""Ingest real SEC 10-K/10-Q filing text into `CompanyDocument` -- the
Document Evidence Engine's ingestion side (see ARCHITECTURE.md). This is
the piece `alpha_lab.ai.service.AIResearchService.ensure_all` was always
missing: it reads `CompanyDocument` rows but nothing wrote them until now.

Mirrors scripts/load_sec_facts.py's CLI shape and its own honest failure
behavior: one ticker's SEC request failing never aborts the rest, and
previously ingested documents are never overwritten or lost.
"""

from pathlib import Path
import argparse
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.ai.documents import ingest_company_documents  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.providers.sec_edgar import SECClient  # noqa: E402
from alpha_lab.providers.sec_filings import SECFilingDocumentProvider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="US ticker symbols")
    parser.add_argument("--user-agent", default=os.getenv("ALPHALAB_SEC_USER_AGENT"))
    args = parser.parse_args()
    if not args.user_agent:
        parser.error("Set --user-agent or ALPHALAB_SEC_USER_AGENT to 'App contact@email'")

    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        provider = SECFilingDocumentProvider(SECClient(args.user_agent))
        for ticker in (value.upper() for value in args.tickers):
            try:
                stored = ingest_company_documents(engine, provider, ticker)
                print(f"{ticker}: stored {stored} new filing document(s)")
            except Exception as error:
                print(f"{ticker}: SEC unavailable ({error}); prior data preserved")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
