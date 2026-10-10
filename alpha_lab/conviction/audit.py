"""Immutable audit trail for Conviction/Conflict Layer outputs.

Write-only from the live system's point of view: `record_assessment` is called
by `scripts/assess_conviction.py` (an explicit operation), and nothing in
`get_research_state`, the dashboard or any scoring path reads the table back.
It is evidence for later validation, never a second source of truth.
"""

import hashlib
import json
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.config.settings import CoverageSettings
from alpha_lab.conviction.assessment import (
    CONVICTION_METHODOLOGY_VERSION,
    ConvictionAssessment,
)
from alpha_lab.database.models import ConvictionAssessmentRecord

if TYPE_CHECKING:
    from alpha_lab.research_state import ResearchState

# Every ResearchState field whose evidence can influence an assessment.
_EVIDENCE_FIELDS = (
    "fundamentals", "analyst_activity", "technicals", "macro", "donatien", "donatien_alignment",
    "ai_research", "ai_rating", "deterministic_score", "news", "news_impact", "ethics",
)


def _canonical(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(data: object) -> str:
    return hashlib.sha256(_canonical(data).encode()).hexdigest()


def input_fingerprint(
    state: "ResearchState", *, coverage: CoverageSettings, stale_price_days: int
) -> str:
    """Identity of the evidence and thresholds an assessment was computed
    from: each evidence field's status + provenance id, the methodology
    version, and the sufficiency thresholds. Same evidence -> same value."""
    return _sha256({
        "methodology": CONVICTION_METHODOLOGY_VERSION,
        "thresholds": {
            "insufficient_below": coverage.insufficient_below,
            "full_confidence_at": coverage.full_confidence_at,
            "stale_price_days": stale_price_days,
        },
        "evidence": {
            name: [getattr(state, name).status, getattr(state, name).provenance_id]
            for name in _EVIDENCE_FIELDS
        },
    })


def content_hash(assessment: ConvictionAssessment) -> str:
    return _sha256(assessment.model_dump(mode="json"))


def _provenance_ids(assessment: ConvictionAssessment) -> list[str]:
    sides = [side for family in assessment.families for side in family.members]
    sides += assessment.context + assessment.synthesis
    sides += [side for conflict in assessment.conflicts for side in conflict.sides]
    return sorted({side.provenance_id for side in sides if side.provenance_id})


def record_assessment(
    engine: Engine, state: "ResearchState", assessment: ConvictionAssessment,
    *, coverage: CoverageSettings, stale_price_days: int,
) -> bool:
    """Append `assessment`; returns False (and writes nothing) when an
    identical assessment for the same ticker/date/methodology already exists."""
    digest = content_hash(assessment)
    with Session(engine) as session:
        already = session.scalar(
            select(ConvictionAssessmentRecord.id).where(
                ConvictionAssessmentRecord.ticker == assessment.ticker,
                ConvictionAssessmentRecord.evaluation_date == assessment.evaluation_date,
                ConvictionAssessmentRecord.methodology_version == assessment.methodology_version,
                ConvictionAssessmentRecord.content_hash == digest,
            )
        )
        if already is not None:
            return False
        session.add(ConvictionAssessmentRecord(
            ticker=assessment.ticker,
            evaluation_date=assessment.evaluation_date,
            methodology_version=assessment.methodology_version,
            agreement=assessment.agreement.value,
            direction=assessment.direction.value,
            assessment=assessment.model_dump(mode="json"),
            conflicts=[conflict.model_dump(mode="json") for conflict in assessment.conflicts],
            provenance_ids=_provenance_ids(assessment),
            input_fingerprint=input_fingerprint(state, coverage=coverage, stale_price_days=stale_price_days),
            content_hash=digest,
        ))
        try:
            session.commit()
        except IntegrityError:  # a concurrent writer recorded the identical row first
            session.rollback()
            return False
    return True


def assessment_history(
    engine: Engine, ticker: str, *, as_of: date | None = None, limit: int | None = None
) -> list[ConvictionAssessmentRecord]:
    """Recorded assessments for `ticker`, newest first. `as_of` is a
    point-in-time bound on when AlphaLab *recorded* them (`recorded_at`), so
    a later write can never appear in an earlier view."""
    query = select(ConvictionAssessmentRecord).where(
        ConvictionAssessmentRecord.ticker == ticker.strip().upper()
    )
    if as_of is not None:
        query = query.where(
            ConvictionAssessmentRecord.recorded_at <= datetime.combine(as_of, time.max, tzinfo=UTC).replace(tzinfo=None)
        )
    query = query.order_by(ConvictionAssessmentRecord.recorded_at.desc(), ConvictionAssessmentRecord.id.desc())
    if limit is not None:
        query = query.limit(limit)
    with Session(engine, expire_on_commit=False) as session:
        return list(session.scalars(query))
