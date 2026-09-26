"""Persistence and point-in-time lookup for `alpha_lab.fx`."""

from datetime import date, datetime, time
import math

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import FXRate
from alpha_lab.database.session import session_scope
from alpha_lab.providers.interfaces import FXRateProvider

USD = "USD"


class FXRateService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure database, no network --------------------------------

    def convert_to_usd(
        self, amount: float | None, currency: str | None, as_of: date
    ) -> float | None:
        """`amount` in `currency`, converted to USD as of `as_of` -- or
        `amount` unchanged when `currency` is `None` (unknown -- most
        `Security` rows predate this field, confirmed live to actually be
        USD) or already `"USD"`, matching the exact backward-compatible
        convention `alpha_lab.scorecard.verdict.classify_tier` and
        `alpha_lab.search.screening._matches` used before real FX
        conversion existed.

        Returns `None` -- never a guess -- when `currency` is explicitly
        known and non-USD but no real FX rate has been ingested for it as
        of this date (`scripts/refresh_fx_rates.py` has not yet run for
        that currency, or `as_of` predates its earliest ingested rate).
        This is the same "missing != fabricated" rule as every other
        optional field in this codebase; a caller comparing the result
        against a USD threshold already treats `None` as unusable, exactly
        like a genuinely-missing `market_cap` today.

        PIT-safe: filters on `FXRate.ingested_at <= end_of(as_of)`, not on
        `FXRate.date` alone -- exactly `get_technical_summary_as_of`'s own
        rationale (`alpha_lab.research.supplemental_service`). A rate
        row dated in the past can still have been inserted only today (a
        first `refresh_fx_rates.py` run ingests real history in one call),
        so filtering on `date` alone could let a historical `as_of` read
        see a rate AlphaLab had not actually stored yet as of that date.
        """
        if amount is None:
            return None
        if currency is None or currency.strip().upper() == USD:
            return amount
        symbol = currency.strip().upper()
        upper_bound = datetime.combine(as_of, time.max)
        with Session(self.engine) as session:
            row = session.scalars(
                select(FXRate)
                .where(
                    FXRate.currency == symbol,
                    FXRate.date <= as_of,
                    FXRate.ingested_at <= upper_bound,
                )
                .order_by(FXRate.date.desc(), FXRate.ingested_at.desc())
                .limit(1)
            ).first()
        return None if row is None else amount * row.rate_to_usd

    # --- refresh: explicit, network -----------------------------------------

    def refresh(
        self, provider: FXRateProvider, currency: str, *, start: date, end: date
    ) -> int:
        """Ingest real daily `currency` -> USD history from `provider` and
        upsert it by `(currency, date)`, mirroring `IngestionService.
        ingest`'s own upsert-by-unique-key pattern for `Price`. A no-op,
        returning 0, for `currency == "USD"` (never a network call -- see
        `FXRateProvider.get_fx_rate_history`'s own docstring) or when the
        provider has no history for this currency at all. Returns the
        number of rows ingested (inserted or updated)."""
        symbol = currency.strip().upper()
        if symbol == USD:
            return 0
        rows = provider.get_fx_rate_history(symbol, start, end)
        if not rows:
            return 0
        ingested = 0
        with session_scope(self.engine) as session:
            for entry in rows:
                rate = entry["rate_to_usd"]
                if not math.isfinite(rate) or rate <= 0:
                    continue
                ingested += 1
                existing = session.scalar(
                    select(FXRate).where(
                        FXRate.currency == symbol, FXRate.date == entry["date"]
                    )
                )
                if existing is not None:
                    existing.rate_to_usd = rate
                    existing.provider = provider.provider_name
                else:
                    session.add(
                        FXRate(
                            currency=symbol,
                            date=entry["date"],
                            rate_to_usd=rate,
                            provider=provider.provider_name,
                        )
                    )
        return ingested
