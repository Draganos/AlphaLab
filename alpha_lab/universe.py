"""Dated history of which securities were in the tracked research universe.

`Security.is_tracked` is a single current flag, so "the tracked universe on
2026-03-01" cannot be recovered from it. The scorer ranks every security
against that universe (a percentile is only meaningful relative to its peers),
so a score for a past date computed against *today's* set changes whenever a
ticker is added or removed. This module records a snapshot of the whole tracked
set whenever it changes (append-only table `universe_snapshots`, written by the
few places that change the flag) and answers "which universe was current on
date D" without inventing history: a date before the first recorded snapshot
has no membership history and says so (`current_fallback`).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alpha_lab.database.models import Security, UniverseSnapshot

BASIS_LIVE = "live"  # today (or later): the current flag, which is the live universe
BASIS_SNAPSHOT = "snapshot"  # a snapshot recorded by the date defines the membership
BASIS_CURRENT_FALLBACK = "current_fallback"  # no membership history by that date: today's set, labelled


@dataclass(frozen=True)
class UniverseAsOf:
    tickers: tuple[str, ...]
    basis: str
    recorded_at: datetime | None = None

    @property
    def point_in_time(self) -> bool:
        return self.basis in (BASIS_LIVE, BASIS_SNAPSHOT)


def current_tracked_tickers(session: Session) -> list[str]:
    return sorted(session.scalars(select(Security.ticker).where(Security.is_tracked.is_(True))))


def record_universe_snapshot(engine: Engine, reason: str) -> bool:
    """Append a snapshot of the current tracked set if it differs from the
    latest one (or none exists and the set is not empty). Returns whether a row
    was written. Call it after any change to `Security.is_tracked` commits."""
    with Session(engine) as session:
        current = current_tracked_tickers(session)
        latest = session.scalar(
            select(UniverseSnapshot).order_by(UniverseSnapshot.recorded_at.desc(), UniverseSnapshot.id.desc())
        )
        if latest is None and not current:
            return False
        if latest is not None and list(latest.tickers) == current:
            return False
        session.add(UniverseSnapshot(
            recorded_at=datetime.now(UTC), tickers=current, size=len(current), reason=reason[:64],
        ))
        session.commit()
        return True


def tracked_universe_as_of(engine: Engine, as_of: date, *, today: date | None = None) -> UniverseAsOf:
    """The tracked universe on `as_of`: the current flag for today or later,
    else the latest snapshot recorded by the end of that day, else today's set
    labelled as a fallback (membership history only exists from the first
    snapshot). Read-only; a database without the table has no snapshots."""
    today = today or date.today()
    with Session(engine) as session:
        current = tuple(current_tracked_tickers(session))
        if as_of >= today:
            return UniverseAsOf(current, BASIS_LIVE)
        try:
            snapshot = session.scalar(
                select(UniverseSnapshot)
                .where(UniverseSnapshot.recorded_at <= datetime.combine(as_of, time.max))
                .order_by(UniverseSnapshot.recorded_at.desc(), UniverseSnapshot.id.desc())
            )
        except OperationalError:  # an older database that has not been migrated yet
            snapshot = None
    if snapshot is None:
        return UniverseAsOf(current, BASIS_CURRENT_FALLBACK)
    return UniverseAsOf(tuple(snapshot.tickers), BASIS_SNAPSHOT, snapshot.recorded_at)
