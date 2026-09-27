#!/usr/bin/env python
"""Explicit, deliberate live research-universe membership management.

The one place a ticker's inclusion in AlphaLab's live research universe
(`Security.is_tracked` -- see that model's own docstring) is added or
removed.

`add` is a full bootstrap, not just price/fundamental ingestion: analyst
consensus, technical summary, AI research, estimates, analyst rating-
change history, and news, then one research rebuild -- every domain the
standalone `scripts/refresh_*.py` scripts would otherwise require running
separately. This exists precisely because adding a ticker via
`IngestionService.ingest` alone (which is what sets `is_tracked=True`)
gives it real but partial coverage -- price/fundamentals only, missing
analyst/technical/AI research entirely -- until someone remembers to run
the rest by hand. One ticker's provider failure at any step never aborts
the others or the rest of the batch.

`remove` only ever flips the flag off. It never deletes the `Security`
row or any historical Price/Fundamental/Estimate/etc. data, so undoing a
`remove` is just an `add` away, and nothing about research history is
ever destroyed.

Neither subcommand touches `scripts/load_universe.py`'s broader catalog
(`Security` rows with `is_tracked=False`) -- that stays a separate, cheap,
untracked directory of tickers AlphaLab merely knows exist.
"""
from datetime import date, timedelta
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import Security  # noqa: E402
from alpha_lab.ingestion import IngestionService  # noqa: E402
from alpha_lab.ingestion.analyst_events import snapshot_analyst_rating_changes  # noqa: E402
from alpha_lab.ingestion.estimates import snapshot_estimates  # noqa: E402
from alpha_lab.news import NewsService  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.research import ResearchService  # noqa: E402
from alpha_lab.research.supplemental_service import SupplementalResearchService  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402

_DEFAULT_INGESTION_YEARS = 5


def add_tickers(engine, settings, tickers: list[str]) -> bool:
    """Full bootstrap for each ticker. Returns False if any ticker's
    price/fundamental ingestion (the step that actually sets
    `is_tracked=True`) failed -- a supplemental-domain failure is reported
    but never fails the whole ticker, mirroring every existing
    `refresh_*.py` script's own per-domain failure isolation.

    Two passes, not one, deliberately: ingestion first for every ticker,
    then one research rebuild, THEN the supplemental (analyst/technical/AI)
    pass. `SupplementalResearchService.refresh_all` -- the only path that
    computes the AI Research Rating -- needs an existing `StockResearch`
    (`ResearchService.get_stock_research`, which reads the persisted
    *current research* snapshot, not a live computation) to synthesize
    from; for a ticker with no prior current-research row at all (every
    ticker `add` is ever called for, by definition), that only starts
    existing once `MarketScreenerService.rebuild_current_research` has run
    at least once since ingestion. Interleaving ingest-then-supplemental
    per ticker in one pass -- the first version of this script did that --
    silently skipped the AI Research Rating for every ticker on its first
    `add`, since `get_stock_research` was always still `None` at that
    point; a repeated `add` for the same ticker would then compute it,
    which is exactly the sign of an ordering bug now fixed here."""
    provider = YFinanceProvider()
    ingestion = IngestionService(provider, engine)
    supplemental = SupplementalResearchService(engine)
    research_service = ResearchService(engine, settings)
    news = NewsService(engine)
    end = date.today()
    start = end - timedelta(days=365 * _DEFAULT_INGESTION_YEARS)

    all_succeeded = True
    ingested: list[str] = []
    for ticker in tickers:
        print(f"--- {ticker}: price/fundamentals ---")
        try:
            ingestion.ingest(ticker, start, end)
        except ProviderError as error:
            print(f"  FAILED ({error.kind.value} - {error.reason}); not added")
            all_succeeded = False
            continue
        print("  ok (now tracked)")
        ingested.append(ticker)

    if ingested:
        print()
        print("Seeding base research for newly-tracked ticker(s)...")
        MarketScreenerService(engine, settings).rebuild_current_research()

    for ticker in ingested:
        print(f"--- {ticker}: supplemental research ---")
        base_research = research_service.get_stock_research(ticker)
        if base_research is None:
            try:
                supplemental.refresh_analyst_consensus(ticker, provider)
            except ProviderError as error:
                print(f"  analyst consensus: FAILED ({error.kind.value} - {error.reason})")
            supplemental.refresh_technical_summary(ticker)
        else:
            result = supplemental.refresh_all(ticker, provider, base_research)
            if result.analyst_error is not None:
                print(
                    f"  analyst consensus: FAILED "
                    f"({result.analyst_error.kind.value} - {result.analyst_error.reason})"
                )
        print("  supplemental research (analyst/technical/AI/fund evidence): ok")

        try:
            observations = provider.get_estimates(ticker, end)
        except ProviderError as error:
            print(f"  estimates: FAILED ({error.kind.value} - {error.reason})")
        else:
            if observations:
                inserted = snapshot_estimates(
                    engine, ticker, end, observations,
                    provider=provider.provider_name,
                    source="yfinance earningsEstimate/revenueEstimate",
                )
                print(f"  estimates: {inserted} new observation(s)")
            else:
                print("  estimates: no coverage (not a failure)")

        try:
            events = provider.get_analyst_rating_changes(ticker)
        except ProviderError as error:
            print(f"  analyst rating-change history: FAILED ({error.kind.value} - {error.reason})")
        else:
            if events:
                inserted = snapshot_analyst_rating_changes(
                    engine, ticker, events,
                    provider=provider.provider_name,
                    source="yfinance upgradeDowngradeHistory",
                )
                print(f"  analyst rating-change history: {inserted} new event(s)")
            else:
                print("  analyst rating-change history: no coverage (not a failure)")

        try:
            news_result = news.refresh(provider, ticker)
        except ProviderError as error:
            print(f"  news: FAILED ({error.kind.value} - {error.reason})")
        else:
            print(f"  news: fetched {news_result.fetched}, stored {news_result.stored} new")

        research_service.snapshot_current_research(ticker)

    print()
    print("Rebuilding current research for the full tracked universe...")
    records = MarketScreenerService(engine, settings).rebuild_current_research()
    print(f"Research rebuilt for {len(records)} tracked securit(y/ies).")
    return all_succeeded


def remove_tickers(engine, settings, tickers: list[str]) -> bool:
    """Flip `is_tracked` off. Never deletes the `Security` row or any
    historical data -- see this module's own docstring. Returns False if
    any named ticker has no `Security` row at all (nothing to remove)."""
    all_found = True
    with Session(engine) as session:
        for ticker in tickers:
            security = session.get(Security, ticker)
            if security is None:
                print(f"{ticker}: not found -- nothing to remove")
                all_found = False
                continue
            if not security.is_tracked:
                print(f"{ticker}: already untracked (no-op)")
                continue
            security.is_tracked = False
            print(f"{ticker}: removed from the tracked universe (history preserved)")
        session.commit()

    print()
    print("Rebuilding current research for the full tracked universe...")
    records = MarketScreenerService(engine, settings).rebuild_current_research()
    print(f"Research rebuilt for {len(records)} tracked securit(y/ies).")
    return all_found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="action", required=True)
    add_parser = subparsers.add_parser(
        "add", help="Add ticker(s) to the tracked research universe (full bootstrap)"
    )
    add_parser.add_argument("tickers", nargs="+")
    remove_parser = subparsers.add_parser(
        "remove", help="Remove ticker(s) from the tracked research universe (history preserved)"
    )
    remove_parser.add_argument("tickers", nargs="+")
    args = parser.parse_args()

    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    tickers = [ticker.upper().strip() for ticker in args.tickers]
    if args.action == "add":
        ok = add_tickers(engine, settings, tickers)
    else:
        ok = remove_tickers(engine, settings, tickers)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
