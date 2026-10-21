"""Point-in-time database queries shared by research and presentation layers."""

from datetime import date, datetime, time

from sqlalchemy import Select, case, func, null, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import Estimate, Fundamental, Price


def latest_fundamentals_as_of_statement(ticker: str, as_of: date) -> Select[tuple[Fundamental]]:
    """Select the newest published version of every fiscal period available at ``as_of``."""
    ranked = (
        select(
            Fundamental.id.label("fundamental_id"),
            func.row_number().over(
                partition_by=(Fundamental.ticker, Fundamental.period),
                order_by=(Fundamental.publication_date.desc(), Fundamental.ingested_at.desc(), Fundamental.id.desc()),
            ).label("version_rank"),
        )
        .where(Fundamental.ticker == ticker, Fundamental.publication_date.is_not(None),
               Fundamental.publication_date <= as_of)
        .subquery()
    )
    return (select(Fundamental).join(ranked, Fundamental.id == ranked.c.fundamental_id)
            .where(ranked.c.version_rank == 1).order_by(Fundamental.period))


def latest_fundamentals_as_of(session: Session, ticker: str, as_of: date) -> list[Fundamental]:
    """Return point-in-time fundamental history without exposing later revisions."""
    return list(session.scalars(latest_fundamentals_as_of_statement(ticker, as_of)))


def latest_estimates_as_of(session: Session, ticker: str, as_of: date) -> list[Estimate]:
    """Return only estimate observations that existed by ``as_of``; no scoring is inferred."""
    ranked = (select(Estimate.id.label("estimate_id"), func.row_number().over(
        partition_by=(Estimate.ticker, Estimate.fiscal_period),
        order_by=(Estimate.observation_date.desc(), Estimate.id.desc())).label("version_rank"))
        .where(Estimate.ticker == ticker, Estimate.observation_date <= as_of).subquery())
    statement = (select(Estimate).join(ranked, Estimate.id == ranked.c.estimate_id)
                 .where(ranked.c.version_rank == 1).order_by(Estimate.fiscal_period))
    return list(session.scalars(statement))


def latest_price_per_date_statement(*conditions) -> Select[tuple[Price]]:
    """Wrap arbitrary `Price` filter `conditions` (e.g. `Price.ticker ==
    ...`, `Price.date <= ...`, optionally `Price.ingested_at <= ...` for a
    PIT-safe caller) so exactly one row survives per calendar `date` -- the
    freshest real revision (max `ingested_at`, ties broken by `id`) --
    mirroring `latest_fundamentals_as_of_statement`/`latest_estimates_as_of`'s
    own row-ranking shape exactly, for the identical reason: `IngestionService.
    ingest` now appends a new `Price` row (rather than mutating one in place)
    whenever a re-ingest genuinely revises an already-stored `(ticker, date)`
    bar (see its own docstring), so more than one row can legitimately exist
    per `(ticker, date)`, distinguished only by `ingested_at`. Every consumer
    of `Price` history must pick exactly one -- the caller decides via
    `conditions` whether "known as of" means today (no ingested_at bound,
    the common case) or a genuinely historical point-in-time reconstruction
    (add an `ingested_at` bound alongside `date`).

    Partitions by `(ticker, date)`, not `date` alone, even though every
    current caller already restricts `conditions` to a single ticker --
    matching `latest_fundamentals_as_of_statement`'s own `(ticker, period)`
    partition exactly, so a future caller that queries across tickers
    without a `Price.ticker == ...` condition still gets one row per
    ticker per date, never two different tickers' same-date rows
    collapsed into one another."""
    ranked = (
        select(
            Price.id.label("price_id"),
            func.row_number().over(
                partition_by=(Price.ticker, Price.date),
                order_by=(Price.ingested_at.desc(), Price.id.desc()),
            ).label("version_rank"),
        )
        .where(*conditions)
        .subquery()
    )
    return (select(Price).join(ranked, Price.id == ranked.c.price_id)
            .where(ranked.c.version_rank == 1).order_by(Price.date))


def latest_price_per_date(session: Session, *conditions) -> list[Price]:
    """Run `latest_price_per_date_statement(*conditions)` and return the list."""
    return list(session.scalars(latest_price_per_date_statement(*conditions)))


def price_history_as_known_statement(known_by: datetime | None, *conditions) -> Select[tuple[Price]]:
    """One `Price` row per `(ticker, date)` as AlphaLab could have seen it at
    `known_by` -- for point-in-time *scoring*, where a later revision of a bar
    must not leak into an earlier date.

    A bar's market date is when it was public; `ingested_at` is only when this
    system stored it, and a first ingestion backfills years of history in one
    go. So the bound applies to *versions of a bar*, never to whether the bar
    exists: among a date's versions the newest one with `ingested_at <=
    known_by` wins; if none was stored that early, the earliest version wins
    (the bar as first recorded -- a backfilled or late-arriving bar is still a
    real bar for its date, and excluding it would empty every date before the
    first ingestion). A strict `ingested_at <= known_by` condition, as the
    technical-summary read uses, is the right bound for "what did AlphaLab hold
    then" but would make this scorer, and every backtest through it, see no
    prices before the day the database was first filled.

    `known_by=None` (a live evaluation) is the newest version of each date:
    exactly `latest_price_per_date_statement`.
    """
    if known_by is None:
        return latest_price_per_date_statement(*conditions)
    known = Price.ingested_at <= known_by
    ranked = (
        select(
            Price.id.label("price_id"),
            func.row_number().over(
                partition_by=(Price.ticker, Price.date),
                order_by=(
                    case((known, 0), else_=1).asc(),  # versions known by then first
                    case((known, Price.ingested_at), else_=null()).desc(),  # ...newest of them
                    case((known, null()), else_=Price.ingested_at).asc(),  # else the earliest version
                    case((known, -Price.id), else_=Price.id).asc(),  # ties: id desc if known, asc if not
                ),
            ).label("version_rank"),
        )
        .where(*conditions)
        .subquery()
    )
    return (select(Price).join(ranked, Price.id == ranked.c.price_id)
            .where(ranked.c.version_rank == 1).order_by(Price.date))


def price_history_as_known(session: Session, as_of: date, *conditions, live: bool | None = None) -> list[Price]:
    """`price_history_as_known_statement` for an evaluation date. A live
    evaluation (`as_of >= today`, the same test `get_research_state` uses) is
    unbounded: the UTC `ingested_at` clock must not exclude a revision stored
    "tomorrow" in UTC but still today locally."""
    is_live = (as_of >= date.today()) if live is None else live
    known_by = None if is_live else datetime.combine(as_of, time.max)
    return list(session.scalars(price_history_as_known_statement(known_by, *conditions)))
