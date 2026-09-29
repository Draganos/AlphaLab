"""Idempotent provider-to-database ingestion."""

from datetime import UTC, date, datetime
import hashlib
import json
import logging
import math
import pandas as pd
from sqlalchemy import Engine, select

from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.session import session_scope
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.ingestion.universe import _canonical_exchange

logger = logging.getLogger(__name__)

# Every field a re-ingest can revise. Order doesn't matter; used both to
# merge an incoming row's non-None fields onto the latest known revision
# and to detect whether anything actually changed -- see ingest()'s Price
# upsert.
_PRICE_FIELDS = (
    "open", "high", "low", "close", "adjusted_close", "volume",
    "currency", "provider", "source",
)


# Relative tolerance below which two numeric price fields are the same bar.
# Measured on the live database: re-fetching an unchanged bar makes the
# provider return `adjusted_close` differing in the last float32 bits
# (every observed jitter <= 5e-7 relative), while every genuine dividend/
# split restatement was >= 1e-3 -- nothing in between -- so 1e-5 separates
# the two with two orders of magnitude of margin on each side. Exact `!=`
# treated the jitter as a revision and appended a duplicate row (14% surplus
# rows after three ingests), growing with every refresh.
_PRICE_RELATIVE_TOLERANCE = 1e-5


def _price_field_changed(new: object, old: object) -> bool:
    if isinstance(new, (int, float)) and isinstance(old, (int, float)):
        return not math.isclose(new, old, rel_tol=_PRICE_RELATIVE_TOLERANCE, abs_tol=1e-9)
    return new != old


class IngestionService:
    def __init__(self, provider: MarketDataProvider, engine: Engine):
        self.provider, self.engine = provider, engine

    def ingest(self, ticker: str, start: date, end: date, *, mark_tracked: bool = True) -> None:
        """Fetch + upsert. `Price` rows are append-only on genuine change --
        an already-stored `(ticker, date)` bar is never mutated in place;
        a real revision (corrected close, split/dividend adjustment
        restating `adjusted_close`, ...) appends a new row with its own
        `ingested_at` instead, mirroring `Fundamental`'s existing
        content-hash-based append-only pattern (and `FXRateService.
        refresh`'s identical fix for the same PIT leak). Mutating in place
        would silently let a later revision leak into a historical PIT
        read (e.g. `get_technical_summary_as_of`) that predates the
        revision -- data AlphaLab did not actually possess at that point
        in its own history. Re-ingesting an unchanged bar is a true
        no-op: nothing is inserted, and the existing row's `ingested_at`
        is left untouched. Every reader of `Price` history must therefore
        pick exactly one row per `(ticker, date)` -- see
        `alpha_lab.database.queries.latest_price_per_date`.

        `mark_tracked=True` (the default) sets `Security.is_tracked = True`
        -- calling `ingest` for a ticker is inherently a deliberate act, so
        this is how a ticker joins AlphaLab's live research universe (see
        `Security`'s own docstring). The one exception is `MacroRegimeService
        .refresh`, which ingests its fixed macro-proxy tickers (VIX,
        Treasury yields, ...) purely for their price history -- those are
        never research candidates, so it passes `mark_tracked=False`.
        `mark_tracked=False` only ever *skips setting* the flag; it never
        clears an already-tracked security's flag back to untracked --
        removal from the universe is exclusively `scripts/manage_universe.
        py remove`'s job."""
        symbol = ticker.upper().strip()
        info = self.provider.get_company_info(symbol)
        info["exchange"] = _canonical_exchange(info.get("exchange"))
        prices = self.provider.get_price_history(symbol, start, end)
        financials = self.provider.get_financials(symbol)
        provider_name = self.provider.provider_name
        currency = info.get("currency")
        with session_scope(self.engine) as session:
            security = session.get(Security, symbol)
            if security is None:
                security = Security(**info)
                session.add(security)
            else:
                for key, value in info.items():
                    if key != "ticker" and value is not None:
                        if key == "exchange" and security.exchange in {
                            "NASDAQ",
                            "NYSE",
                        }:
                            continue
                        setattr(security, key, value)
            if mark_tracked:
                security.is_tracked = True
            security.metadata_updated_at = datetime.now(UTC)
            for index, row in prices.iterrows():
                values = {
                    key: self._number(row.get(key))
                    for key in [
                        "open",
                        "high",
                        "low",
                        "close",
                        "adjusted_close",
                        "volume",
                    ]
                }
                values.update(
                    currency=currency,
                    provider=provider_name,
                    source=self._text(row.get("source")),
                )
                price_date = pd.Timestamp(index).date()
                latest = session.scalars(
                    select(Price)
                    .where(Price.ticker == symbol, Price.date == price_date)
                    .order_by(Price.ingested_at.desc(), Price.id.desc())
                    .limit(1)
                ).first()
                merged = {
                    field: values[field]
                    if values.get(field) is not None
                    else (getattr(latest, field) if latest else None)
                    for field in _PRICE_FIELDS
                }
                if latest is None or any(
                    _price_field_changed(merged[field], getattr(latest, field))
                    for field in _PRICE_FIELDS
                ):
                    session.add(Price(ticker=symbol, date=price_date, **merged))
            for row in financials.to_dict("records"):
                period = pd.Timestamp(row.pop("period")).date()
                values = {
                    key: (
                        self._date(value)
                        if key == "publication_date"
                        else self._number(value)
                    )
                    for key, value in row.items()
                    if key != "source"
                }
                source = row.get("source")
                values.update(
                    currency=currency,
                    provider=provider_name,
                    source=None if source is None or pd.isna(source) else str(source),
                )
                observation_hash = self._fundamental_hash(symbol, period, values)
                existing = (
                    session.query(Fundamental.id)
                    .filter_by(observation_hash=observation_hash)
                    .one_or_none()
                )
                if existing is None:
                    session.add(
                        Fundamental(
                            ticker=symbol,
                            period=period,
                            observation_hash=observation_hash,
                            **values,
                        )
                    )
        logger.info(
            "ingestion_complete",
            extra={
                "ticker": symbol,
                "prices": len(prices),
                "fundamentals": len(financials),
            },
        )

    @staticmethod
    def _number(value):
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @staticmethod
    def _text(value):
        return None if value is None or pd.isna(value) else str(value)

    @staticmethod
    def _date(value):
        return None if value is None or pd.isna(value) else pd.Timestamp(value).date()

    @staticmethod
    def _fundamental_hash(ticker: str, period: date, values: dict) -> str:
        """Identify an exact provider observation while excluding ingestion time."""
        canonical = {"ticker": ticker, "period": period.isoformat(), **values}
        payload = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()
