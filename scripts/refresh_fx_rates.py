#!/usr/bin/env python
"""Explicitly refresh real point-in-time currency -> USD FX rates for every
non-USD currency actually present among currently-ingested Security rows.

Never fetches a currency speculatively -- only ones real ingested data
actually needs, read live from the database each run (so a newly-ingested
non-USD universe, e.g. config/default.yaml's `universe.uae`, starts getting
real FX history the next time this script runs, with no code change).
Ingests two years of daily history per currency via YFinanceProvider's
`<CUR>USD=X` FX pair (see `alpha_lab.fx.FXRateService`/
`YFinanceProvider.get_fx_rate_history`), independent of --
and never blocked by -- that currency's own securities' price/fundamental
ingestion.

A currency that fails to ingest (ProviderError, or Yahoo Finance simply
having no FX pair for it) does not abort the rest of the run -- it is
reported as failed/empty, and any previously-ingested rate history for it
is left untouched. `MarketScreenerService`/`classify_tier`/screening's
market-cap filters already treat a currency with no ingested rate as
unusable (`market_cap_usd=None`), never as a reason to crash.
"""
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.database.models import Security  # noqa: E402
from alpha_lab.fx import FXRateService  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402

DEFAULT_LOOKBACK_YEARS = 2


def _non_usd_currencies(engine) -> list[str]:
    with Session(engine) as session:
        currencies = session.scalars(
            select(Security.currency).distinct()
        ).all()
    return sorted(
        {
            currency.strip().upper()
            for currency in currencies
            if currency and currency.strip().upper() != "USD"
        }
    )


def main() -> int:
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    currencies = _non_usd_currencies(engine)
    if not currencies:
        print("No non-USD currencies among currently-ingested securities -- nothing to refresh.")
        return 0

    end = date.today()
    start = end - timedelta(days=365 * DEFAULT_LOOKBACK_YEARS)
    service = FXRateService(engine)
    provider = YFinanceProvider()

    succeeded: dict[str, int] = {}
    failed: dict[str, str] = {}
    for currency in currencies:
        try:
            succeeded[currency] = service.refresh(provider, currency, start=start, end=end)
        except ProviderError as error:
            failed[currency] = str(error)

    print(f"AlphaLab FX rates refreshed for {len(currencies)} currencies.")
    for currency, count in succeeded.items():
        print(f"  {currency}: {count} rate(s) ingested/updated")
    for currency, error in failed.items():
        print(f"  {currency}: FAILED -- {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
