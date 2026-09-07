"""Persistence/orchestration for Phase 2B Donatien <-> Market Regime
Alignment.

Deliberately separate from `alpha_lab.research`/`alpha_lab.screener`/
`alpha_lab.strategy`/`alpha_lab.backtest`/`alpha_lab.portfolio`: nothing in
any of those modules imports from here, and this module never touches
`StockResearch`, `LiveResearchRecord`, `composite_score`, or ranking -- see
`alpha_lab.alignment.alignment`'s module docstring for the full scope
rationale.

Unlike `alpha_lab.macro`/`alpha_lab.calibration`, this module has no
provider and makes no network call at all, ever -- it only reads two
already-computed, already-persisted evidence layers
(`alpha_lab.macro.service.MacroRegimeService`,
`alpha_lab.calibration.service.ExternalCalibrationService`) and computes a
categorical comparison. "refresh" here means "recompute the alignment from
currently stored evidence," never "fetch new data."

Read methods here are pure database reads -- safe to call from a Streamlit
render. `refresh` is meant to be triggered explicitly (a script or a UI
button), consistent with the rest of the explicit-refresh architecture,
even though it makes no network call itself.
"""

from datetime import UTC, date, datetime
import hashlib
import json

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.alignment.alignment import AlignmentAssessment, build_alignment_assessment
from alpha_lab.calibration.service import DEFAULT_CALIBRATION_SOURCE, ExternalCalibrationService
from alpha_lab.database.models import AlignmentAssessmentSnapshot, CurrentAlignmentAssessment
from alpha_lab.macro.regime import MacroAssessment
from alpha_lab.macro.service import DEFAULT_MACRO_SCOPE, MacroRegimeService
from alpha_lab.providers.donatien import DonatienCalibration


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _content_hash(assessment: AlignmentAssessment) -> str:
    """Hash only the substantive alignment content -- deliberately excludes
    every date/timestamp field (`as_of`, `market_as_of`, `donatien_run_date`,
    `donatien_source_observed_at`, `donatien_retrieved_at`) for the same
    reason `alpha_lab.macro.service._content_hash` excludes `as_of`: a
    freshness marker advancing by itself (a day passing with nothing
    substantive changing on either side) is not a change in what was
    actually observed, and must not defeat hash-based deduplication."""
    substantive = {
        "alignment": assessment.alignment.value,
        "methodology_version": assessment.methodology_version,
        "market_regime": None if assessment.market_regime is None else assessment.market_regime.value,
        "market_regime_coverage": assessment.market_regime_coverage,
        "donatien_lean": None if assessment.donatien_lean is None else assessment.donatien_lean.value,
        "donatien_dominant_regime": assessment.donatien_dominant_regime,
        "donatien_confidence": assessment.donatien_confidence,
        "donatien_defensiveness": assessment.donatien_defensiveness,
        "donatien_scenario_weights": assessment.donatien_scenario_weights,
    }
    return hashlib.sha256(_canonical_json(substantive).encode()).hexdigest()


def _snapshot_id(*, scope: str, content_hash: str) -> str:
    identity = {"scope": scope, "content_hash": content_hash}
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


class AlignmentService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation -------------------------

    def get_current(self, scope: str = DEFAULT_MACRO_SCOPE) -> CurrentAlignmentAssessment | None:
        with Session(self.engine) as session:
            row = session.get(CurrentAlignmentAssessment, scope)
            if row is not None:
                session.expunge(row)
            return row

    def get_current_assessment(self, scope: str = DEFAULT_MACRO_SCOPE) -> AlignmentAssessment | None:
        row = self.get_current(scope)
        return None if row is None else AlignmentAssessment.model_validate(row.payload)

    def get_history(
        self, scope: str = DEFAULT_MACRO_SCOPE, *, limit: int | None = None
    ) -> list[AlignmentAssessmentSnapshot]:
        with Session(self.engine) as session:
            statement = (
                select(AlignmentAssessmentSnapshot)
                .where(AlignmentAssessmentSnapshot.scope == scope)
                .order_by(AlignmentAssessmentSnapshot.created_at.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            session.expunge_all()
            return list(rows)

    # --- refresh: pure recomputation from already-stored evidence -----------

    def refresh(
        self,
        *,
        scope: str = DEFAULT_MACRO_SCOPE,
        source: str = DEFAULT_CALIBRATION_SOURCE,
        as_of: date | None = None,
    ) -> CurrentAlignmentAssessment:
        """Recompute the alignment for `as_of` (default today) from
        whatever Market Regime / Donatien evidence was already knowable by
        then. No network call.

        Uses `MacroRegimeService.get_assessment_as_of` and
        `ExternalCalibrationService.get_calibration_as_of` unconditionally
        (even for `as_of=today`) so there is exactly one point-in-time-safe
        code path for both current and historical alignment, never a
        separate "current" shortcut that could drift from it.
        """
        as_of = as_of or date.today()
        macro_service = MacroRegimeService(self.engine)
        calibration_service = ExternalCalibrationService(self.engine)

        macro_row = macro_service.get_assessment_as_of(scope, as_of=as_of)
        calibration_row = calibration_service.get_calibration_as_of(source, as_of=as_of)

        market = None if macro_row is None else MacroAssessment.model_validate(macro_row.payload)
        donatien = (
            None
            if calibration_row is None
            else DonatienCalibration.model_validate(calibration_row.normalized_payload)
        )
        donatien_retrieved_at = None if calibration_row is None else calibration_row.retrieved_at

        assessment = build_alignment_assessment(
            as_of=as_of, market=market, donatien=donatien, donatien_retrieved_at=donatien_retrieved_at
        )
        payload = assessment.model_dump(mode="json")
        content_hash = _content_hash(assessment)

        with Session(self.engine) as session:
            existing = session.get(CurrentAlignmentAssessment, scope)
            content_changed = existing is None or existing.content_hash != content_hash

            if content_changed:
                snapshot_id = _snapshot_id(scope=scope, content_hash=content_hash)
                already_present = session.scalar(
                    select(AlignmentAssessmentSnapshot).where(
                        AlignmentAssessmentSnapshot.snapshot_id == snapshot_id
                    )
                )
                if already_present is None:
                    session.add(AlignmentAssessmentSnapshot(
                        snapshot_id=snapshot_id, scope=scope, as_of=as_of,
                        alignment=assessment.alignment.value,
                        methodology_version=assessment.methodology_version,
                        content_hash=content_hash, payload=payload,
                    ))
                    try:
                        session.flush()
                    except IntegrityError:
                        # A concurrent refresh persisted identical content
                        # first; the unique constraint on snapshot_id is the
                        # actual idempotency guarantee.
                        session.rollback()
                        existing = session.get(CurrentAlignmentAssessment, scope)

            if existing is None:
                existing = CurrentAlignmentAssessment(scope=scope)
                session.add(existing)
            existing.as_of = as_of
            existing.alignment = assessment.alignment.value
            existing.methodology_version = assessment.methodology_version
            existing.content_hash = content_hash
            existing.payload = payload
            existing.computed_at = datetime.now(UTC)

            session.commit()
            session.refresh(existing)
            session.expunge(existing)
            return existing
