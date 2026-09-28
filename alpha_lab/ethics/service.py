"""Automatic deterministic ethical evaluation from stored company metadata."""

from datetime import UTC, date, datetime, time

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import EthicalEvaluation, Security
from alpha_lab.ethics.policy import (
    BusinessEvidence,
    EthicsPolicy,
    evaluate_business,
    evidence_fingerprint,
)


class EthicalClassificationService:
    """Persist a new decision only when metadata evidence or policy changes."""

    def __init__(self, engine: Engine, policy: EthicsPolicy):
        self.engine = engine
        self.policy = policy

    def get_evaluation_as_of(self, ticker: str, *, as_of: date) -> EthicalEvaluation | None:
        """Point-in-time historical lookup: the most recent evaluation that
        genuinely existed by `as_of`, filtered on `evaluated_at` (when
        AlphaLab actually classified this ticker) -- never on any later
        field. Pure read, no network, never writes -- mirrors
        `alpha_lab.alignment.service.AlignmentService.get_assessment_as_of`/
        `alpha_lab.calibration.service.ExternalCalibrationService.
        get_calibration_as_of` exactly, for the same reason: `ensure_
        security` is a write-triggering method (evaluates and persists a
        new decision when evidence or policy changed), never safe to call
        merely to answer "what was this ticker's ethical status on a past
        date." Returns `None` when no evaluation existed yet by `as_of`,
        never a guess."""
        upper_bound = datetime.combine(as_of, time.max)
        with Session(self.engine) as session:
            row = session.scalar(
                select(EthicalEvaluation)
                .where(EthicalEvaluation.ticker == ticker, EthicalEvaluation.evaluated_at <= upper_bound)
                .order_by(EthicalEvaluation.evaluated_at.desc(), EthicalEvaluation.id.desc())
            )
            if row is not None:
                session.expunge(row)
            return row

    def ensure_all(self) -> dict[str, str]:
        """Only the tracked research universe (`Security.is_tracked`) --
        never `scripts/load_universe.py`'s broader untracked catalog, which
        can hold thousands of rows with no research relationship to
        AlphaLab at all. See `Security`'s own docstring."""
        with Session(self.engine) as session:
            securities = list(
                session.scalars(
                    select(Security).where(Security.is_tracked.is_(True)).order_by(Security.ticker)
                )
            )
        return {
            security.ticker: self.ensure_security(security) for security in securities
        }

    def ensure_security(self, security: Security) -> str:
        evidence = business_evidence_from_security(security)
        fingerprint = evidence_fingerprint(evidence)
        with Session(self.engine) as session:
            latest = session.scalar(
                select(EthicalEvaluation)
                .where(EthicalEvaluation.ticker == security.ticker)
                .order_by(
                    EthicalEvaluation.evaluated_at.desc(), EthicalEvaluation.id.desc()
                )
            )
            if (
                latest is not None
                and latest.policy_version == self.policy.deterministic_version
                and latest.evidence_fingerprint == fingerprint
            ):
                return latest.ethical_status
        decision = evaluate_business(evidence, self.policy, datetime.now(UTC))
        with Session(self.engine) as session:
            session.add(
                EthicalEvaluation(
                    ticker=decision.ticker,
                    ethical_status=decision.ethical_status.value,
                    primary_business=decision.primary_business,
                    business_tags=decision.business_tags,
                    exclusion_reasons=decision.exclusion_reasons,
                    review_reasons=decision.review_reasons,
                    evidence=decision.evidence,
                    source=decision.source,
                    evaluated_at=decision.evaluated_at,
                    policy_version=decision.policy_version,
                    manual_override=decision.manual_override,
                    manual_override_reason=decision.manual_override_reason,
                    financial_warnings=decision.financial_warnings,
                    evidence_fingerprint=decision.evidence_fingerprint,
                )
            )
            session.commit()
        return decision.ethical_status.value


def business_evidence_from_security(security: Security) -> BusinessEvidence:
    description = security.business_description
    source = (
        security.metadata_source
        or security.metadata_provider
        or "stored-security-metadata"
    )
    evidence = []
    if description:
        evidence.append({"source": source, "text": description})
    tags = []
    combined = " ".join(
        value or "" for value in (description, security.industry)
    ).casefold()
    if "payment" in combined:
        tags.append("payment_processing")
    if "airline" in combined or "passenger aviation" in combined:
        tags.append("airlines")
    warnings = []
    if security.sector == "Financials":
        warnings.append(
            "Financial-sector classification requires explicit business-activity evidence"
        )
    return BusinessEvidence(
        ticker=security.ticker,
        primary_business=description,
        business_tags=tags,
        evidence=evidence,
        source=source,
        financial_warnings=warnings,
        sector=security.sector,
        industry=security.industry,
    )
