#!/usr/bin/env python
"""Explicitly refresh AlphaLab News Engine evidence for one or more tickers.

Fetches recent news via YFinanceProvider.get_news, validates and
hash-deduplicates each article, and appends only genuinely new articles.
Never fabricates a missing title/url/timestamp -- a malformed article is
skipped (counted, not silently dropped) rather than the whole refresh
aborting. A failed refresh (or one ticker's failure) leaves every
previously stored article for every ticker completely untouched.

Coverage caveat: this can only capture news from the point it is run
onward -- it cannot retroactively backfill news from before a ticker was
first refreshed. See alpha_lab.news.service's module docstring.
"""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.news import NewsService  # noqa: E402
from alpha_lab.providers import YFinanceProvider  # noqa: E402
from alpha_lab.providers.errors import ProviderError  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh AlphaLab News Engine evidence")
    parser.add_argument("tickers", nargs="+", help="US ticker symbols")
    args = parser.parse_args()

    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    service = NewsService(engine)
    provider = YFinanceProvider()
    for ticker in (value.upper() for value in args.tickers):
        try:
            result = service.refresh(provider, ticker)
        except ProviderError as error:
            print(f"{ticker}: not refreshed ({error.kind.value} -- {error.reason}); prior articles preserved")
            continue
        print(
            f"{ticker}: fetched {result.fetched}, stored {result.stored} new, "
            f"{result.duplicates} already known, {result.invalid} invalid/skipped"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
