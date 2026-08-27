"""Point-in-time database queries shared by research and presentation layers."""

from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import Fundamental


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
