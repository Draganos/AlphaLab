#!/usr/bin/env python
"""Explicit, deliberate live research-universe membership management.

The one place a ticker's inclusion in AlphaLab's live research universe
(`Security.is_tracked` -- see that model's own docstring) is added or
removed.

`add` is a full bootstrap, not just price/fundamental ingestion: SEC
filings (equities only, when `ALPHALAB_SEC_USER_AGENT` is set -- the input
to AI Research), analyst consensus, technical summary, AI research
rating, estimates, analyst rating-change history AND estimate revision
trend, and news, then one research rebuild -- every domain the standalone
`scripts/refresh_*.py` scripts would otherwise require running
separately. (An earlier version silently omitted the revision trend and
filings, leaving every ticker added this way with permanently empty
"Revisions" and "AI Research" coverage while this docstring claimed
otherwise; a skipped domain is now always reported explicitly.) This exists precisely because adding a ticker via
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
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.ai.documents import ingest_company_documents  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import Security  # noqa: E402
from alpha_lab.ingestion import IngestionService  # noqa: E402
from alpha_lab.ingestion.estimates import snapshot_estimates  # noqa: E402
from alpha_lab.news import NewsService  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.refresh import (  # noqa: E402
    UniverseTooLargeToAdopt,
    adopt_current_research_tickers,
    set_tracked_tickers,
)
from alpha_lab.providers.sec_edgar import SECClient  # noqa: E402
from alpha_lab.providers.sec_filings import SECFilingDocumentProvider  # noqa: E402
from alpha_lab.research import ResearchService  # noqa: E402
from alpha_lab.research.analyst_events import AnalystEventsService  # noqa: E402
from alpha_lab.research.security_type import SecurityType, normalize_security_type  # noqa: E402
from alpha_lab.research.supplemental_service import SupplementalResearchService  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402

_DEFAULT_INGESTION_YEARS = 5


def _ingest_filings(engine, ticker: str) -> None:
    """SEC 10-K/10-Q text -> `CompanyDocument`, the only input to the
    AI Research category. Never silent: an ETF (no filings exist, and the
    category is structurally not-applicable to it) and a missing SEC user
    agent are each reported explicitly rather than skipped without a
    word. Must run before the research rebuild, which is what turns new
    documents into an `AIResearchAnalysis`."""
    with Session(engine) as session:
        security = session.get(Security, ticker)
        asset_type = None if security is None else security.asset_type
    if normalize_security_type(asset_type) is SecurityType.ETF:
        print("  SEC filings: not applicable (ETF -- funds file no 10-K/10-Q)")
        return
    user_agent = os.getenv("ALPHALAB_SEC_USER_AGENT")
    if not user_agent:
        print(
            "  SEC filings: SKIPPED -- ALPHALAB_SEC_USER_AGENT is not set, so "
            "AI Research coverage stays empty for this ticker. Set it to "
            "'App contact@email' and run scripts/refresh_company_documents.py "
            f"{ticker}."
        )
        return
    try:
        stored = ingest_company_documents(engine, SECFilingDocumentProvider(SECClient(user_agent)), ticker)
    except Exception as error:  # noqa: BLE001 -- same isolation as refresh_company_documents.py
        print(f"  SEC filings: FAILED ({error}); prior data preserved")
    else:
        print(f"  SEC filings: stored {stored} new filing document(s)")


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
        _ingest_filings(engine, ticker)

    if ingested:
        print()
        print("Seeding base research for newly-tracked ticker(s)...")
        MarketScreenerService(engine, settings).rebuild_current_research()

    for ticker in ingested:
        print(f"--- {ticker}: supplemental research ---")
        base_research = research_service.get_stock_research(ticker)
        supplemental_ok = True
        if base_research is None:
            try:
                supplemental.refresh_analyst_consensus(ticker, provider)
            except ProviderError as error:
                supplemental_ok = False
                print(f"  analyst consensus: FAILED ({error.kind.value} - {error.reason})")
            supplemental.refresh_technical_summary(ticker)
        else:
            result = supplemental.refresh_all(ticker, provider, base_research)
            if result.analyst_error is not None:
                supplemental_ok = False
                print(
                    f"  analyst consensus: FAILED "
                    f"({result.analyst_error.kind.value} - {result.analyst_error.reason})"
                )
        if supplemental_ok:
            print("  supplemental research (analyst/technical/AI/fund evidence): ok")
        else:
            print("  supplemental research: INCOMPLETE (see failure above; AI rating not refreshed)")

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

        outcome = AnalystEventsService(engine).refresh_all(ticker, provider)
        if outcome.rating_changes_error is not None:
            error = outcome.rating_changes_error
            print(f"  analyst rating-change history: FAILED ({error.kind.value} - {error.reason})")
        else:
            print(f"  analyst rating-change history: {outcome.rating_changes_stored} new event(s)")
        if outcome.revision_trend_error is not None:
            error = outcome.revision_trend_error
            print(f"  estimate revision trend: FAILED ({error.kind.value} - {error.reason})")
        else:
            print(f"  estimate revision trend: {outcome.revision_trend_stored} new observation(s)")

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


def adopt_current(engine) -> bool:
    """Re-track every security in the latest current research build. The
    repair for a database whose `is_tracked` column was added without a
    backfill (every security became untracked, so Full Refresh ingested
    0/0). Adds only; never removes; no network."""
    try:
        adopted = adopt_current_research_tickers(engine)
    except UniverseTooLargeToAdopt as error:
        print(error)
        return False
    if adopted:
        print(f"Re-tracked {len(adopted)} securit(y/ies) from the latest research build: {', '.join(adopted)}")
        return True
    print(
        "Nothing to adopt: the latest research build has no untracked securities "
        "(or no build exists). Use `add TICKER ...` to start tracking a ticker."
    )
    return False


def set_tracked(engine, settings, tickers: list[str]) -> bool:
    """Make the tracked universe exactly `tickers`. Flags only -- nothing is
    deleted or fetched -- then one research rebuild so the dashboard reflects
    it. A ticker with no `Security` row is reported: it needs `add`."""
    try:
        result = set_tracked_tickers(engine, tickers)
    except ValueError as error:
        print(error)
        return False
    print(f"Tracked universe is now exactly {len(result.tracked)} securit(y/ies): {', '.join(result.tracked)}")
    if result.newly_tracked:
        print(f"  newly tracked: {', '.join(result.newly_tracked)}")
    print(f"  untracked {result.untracked_count} other securit(y/ies) (history preserved)")
    if result.missing:
        print(f"  NOT FOUND (run `add {' '.join(result.missing)}` to ingest them): {', '.join(result.missing)}")
    print()
    print("Rebuilding current research for the tracked universe...")
    records = MarketScreenerService(engine, settings).rebuild_current_research()
    print(f"Research rebuilt for {len(records)} tracked securit(y/ies).")
    return not result.missing


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
    subparsers.add_parser(
        "adopt-current",
        help="Re-track every security in the latest research build (repair for an empty tracked universe)",
    )
    set_parser = subparsers.add_parser(
        "set-tracked",
        help="Make the tracked universe exactly these tickers (untracks all others; history preserved)",
    )
    set_parser.add_argument("tickers", nargs="+")
    remove_parser = subparsers.add_parser(
        "remove", help="Remove ticker(s) from the tracked research universe (history preserved)"
    )
    remove_parser.add_argument("tickers", nargs="+")
    args = parser.parse_args()

    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    if args.action == "set-tracked":
        return 0 if set_tracked(engine, settings, [t.upper().strip() for t in args.tickers]) else 1
    if args.action == "adopt-current":
        return 0 if adopt_current(engine) else 1
    tickers = [ticker.upper().strip() for ticker in args.tickers]
    if args.action == "add":
        ok = add_tickers(engine, settings, tickers)
    else:
        ok = remove_tickers(engine, settings, tickers)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
