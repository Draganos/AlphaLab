"""Persistence/orchestration for Donatien External Calibration.

Deliberately separate from ``alpha_lab.research``/``alpha_lab.screener``/
``alpha_lab.strategy``/``alpha_lab.backtest``: nothing in any of those
modules imports from here, and nothing here imports ``StockResearch``,
``LiveResearchRecord``, ``composite_score``, ``HistoricalScoringService``, or
``BacktestEngine``. Per the project brief, Donatien is EXTERNAL_CALIBRATION,
not ground truth and not a stock-rating input -- Phase 1 is fetch, validate,
timestamp, hash, persist, expose, and nothing else. There is no code path
from this module into any ranking or scoring calculation.

Read methods here are pure database reads (no provider, no computation) --
safe to call from a Streamlit render. ``refresh`` calls the provider and is
meant to be triggered explicitly (a script), never from an ordinary page
render.
"""

from datetime import date, datetime, time

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.database.models import CurrentExternalCalibration, ExternalCalibrationSnapshot
from alpha_lab.providers.donatien import (
    DONATIEN_METHODOLOGY_VERSION,
    DonatienCalibration,
    DonatienProvider,
    source_observed_at,
)

DEFAULT_CALIBRATION_SOURCE = "Donatien"


def _canonical_json(data: object) -> str:
    import json

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _snapshot_id(*, source: str, content_hash: str) -> str:
    """Deterministic identity for one historical observation. Deliberately
    excludes retrieved_at/created_at -- those are AlphaLab-side timestamps,
    not part of what Donatien actually said, so refetching the same content
    at a different retrieval time is idempotent rather than a duplicate."""
    import hashlib

    identity = {"source": source, "content_hash": content_hash}
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


class ExternalCalibrationService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation -------------------------

    def get_current(
        self, source: str = DEFAULT_CALIBRATION_SOURCE
    ) -> CurrentExternalCalibration | None:
        """The latest successfully validated calibration row for `source`,
        or None if it has never been refreshed. Returns the raw row (not
        just the parsed calibration) so callers/UI can read
        content_hash/timestamps/raw_payload alongside the normalized data
        without a second query."""
        with Session(self.engine) as session:
            row = session.get(CurrentExternalCalibration, source)
            if row is not None:
                session.expunge(row)
            return row

    def get_current_calibration(
        self, source: str = DEFAULT_CALIBRATION_SOURCE
    ) -> DonatienCalibration | None:
        row = self.get_current(source)
        return None if row is None else DonatienCalibration.model_validate(row.normalized_payload)

    def get_history(
        self,
        source: str = DEFAULT_CALIBRATION_SOURCE,
        *,
        limit: int | None = None,
    ) -> list[ExternalCalibrationSnapshot]:
        """Historical snapshots for `source`, newest first. Read-only;
        never touches CurrentExternalCalibration."""
        with Session(self.engine) as session:
            statement = (
                select(ExternalCalibrationSnapshot)
                .where(ExternalCalibrationSnapshot.source == source)
                .order_by(ExternalCalibrationSnapshot.created_at.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            session.expunge_all()
            return list(rows)

    def get_calibration_as_of(
        self, source: str = DEFAULT_CALIBRATION_SOURCE, *, as_of: date
    ) -> ExternalCalibrationSnapshot | None:
        """Point-in-time historical lookup. Filters on `retrieved_at` (when
        AlphaLab's own refresh actually observed this content), never on
        Donatien's self-reported `source_run_date`/`macro_report_date` --
        mirroring `alpha_lab.database.queries.latest_fundamentals_as_of`'s
        publication_date-based filtering: was this knowable by `as_of`, not
        what period it describes. Filtering on source_run_date instead
        would risk look-ahead bias -- a Donatien report dated before
        `as_of` that AlphaLab did not actually retrieve until after `as_of`
        must never be used for that `as_of`.
        """
        upper_bound = datetime.combine(as_of, time.max)
        with Session(self.engine) as session:
            row = session.scalars(
                select(ExternalCalibrationSnapshot)
                .where(
                    ExternalCalibrationSnapshot.source == source,
                    ExternalCalibrationSnapshot.retrieved_at <= upper_bound,
                )
                .order_by(ExternalCalibrationSnapshot.retrieved_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                session.expunge(row)
            return row

    # --- refresh: explicit, provider call happens before any DB write ------

    def refresh(
        self, provider: DonatienProvider, *, source: str = DEFAULT_CALIBRATION_SOURCE
    ) -> CurrentExternalCalibration:
        """Fetch -> validate -> normalize -> compare hash -> write.

        `provider.fetch()` raises ``ProviderError`` on any failure (network,
        missing cal-json container, malformed JSON, or schema validation)
        *before* this method touches the database -- exactly mirroring
        ``alpha_lab.research.supplemental_service``'s fetch-before-write
        pattern, so a failed refresh always leaves the previous valid
        current state (and all history) completely untouched.

        Unchanged content (same `content_hash` as the current row) updates
        `retrieved_at`/`updated_at` on the current row but never inserts a
        duplicate historical snapshot. Changed content always appends a new
        immutable historical snapshot before updating the current row.
        """
        result = provider.fetch()  # may raise ProviderError; nothing written yet
        observed_at = source_observed_at(result.calibration)
        normalized_payload = result.calibration.model_dump(mode="json")

        with Session(self.engine) as session:
            existing = session.get(CurrentExternalCalibration, source)
            content_changed = existing is None or existing.content_hash != result.content_hash

            if content_changed:
                snapshot_id = _snapshot_id(source=source, content_hash=result.content_hash)
                snapshot_row = session.scalar(
                    select(ExternalCalibrationSnapshot).where(
                        ExternalCalibrationSnapshot.snapshot_id == snapshot_id
                    )
                )
                if snapshot_row is None:
                    session.add(
                        ExternalCalibrationSnapshot(
                            snapshot_id=snapshot_id,
                            source=source,
                            content_hash=result.content_hash,
                            source_run_date=result.calibration.run_date,
                            source_run_time_raw=result.calibration.run_time,
                            source_observed_at=observed_at,
                            retrieved_at=result.retrieved_at,
                            supersedes=result.calibration.supersedes,
                            schema_version=DONATIEN_METHODOLOGY_VERSION,
                            source_url=result.source_url,
                            raw_payload=result.raw_payload,
                            normalized_payload=normalized_payload,
                        )
                    )
                    try:
                        session.flush()
                    except IntegrityError:
                        # A concurrent refresh persisted identical content
                        # first; the unique constraint on snapshot_id is the
                        # actual idempotency guarantee.
                        session.rollback()
                        existing = session.get(CurrentExternalCalibration, source)

            if existing is None:
                existing = CurrentExternalCalibration(source=source)
                session.add(existing)
            existing.content_hash = result.content_hash
            existing.source_run_date = result.calibration.run_date
            existing.source_run_time_raw = result.calibration.run_time
            existing.source_observed_at = observed_at
            existing.retrieved_at = result.retrieved_at
            existing.supersedes = result.calibration.supersedes
            existing.schema_version = DONATIEN_METHODOLOGY_VERSION
            existing.source_url = result.source_url
            existing.raw_payload = result.raw_payload
            existing.normalized_payload = normalized_payload
            existing.updated_at = result.retrieved_at

            session.commit()
            session.refresh(existing)
            session.expunge(existing)
            return existing
