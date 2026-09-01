"""Automatic deterministic ethical evaluation from stored company metadata."""

from datetime import UTC, datetime

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

    def ensure_all(self) -> dict[str, str]:
        with Session(self.engine) as session:
            securities = list(
                session.scalars(select(Security).order_by(Security.ticker))
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
