"""Persistence for Phase 3 evidence, ethical decisions, themes, AI research, and screens."""

from datetime import UTC, datetime

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from alpha_lab.ai import AIResearchResult
from alpha_lab.database.models import (
    AIResearchAnalysis,
    BusinessTheme,
    EthicalEvaluation,
    SavedScreener,
)
from alpha_lab.ethics import EthicalDecision
from alpha_lab.themes import ThemeEvidence


class Phase3Repository:
    """Small repository that returns usable detached records."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def save_ethics(self, decision: EthicalDecision) -> EthicalEvaluation:
        with Session(self.engine, expire_on_commit=False) as session:
            existing = session.scalar(
                select(EthicalEvaluation).where(
                    EthicalEvaluation.ticker == decision.ticker,
                    EthicalEvaluation.evaluated_at == decision.evaluated_at,
                    EthicalEvaluation.policy_version == decision.policy_version,
                )
            )
            if existing is not None:
                return existing
            record = EthicalEvaluation(
                ticker=decision.ticker,
                ethical_status=decision.ethical_status.value,
                primary_business=decision.primary_business,
                business_tags=decision.business_tags,
                exclusion_reasons=decision.exclusion_reasons,
                review_reasons=decision.review_reasons,
                evidence=list(decision.evidence),
                source=decision.source,
                evaluated_at=decision.evaluated_at,
                policy_version=decision.policy_version,
                manual_override=decision.manual_override,
                manual_override_reason=decision.manual_override_reason,
                financial_warnings=decision.financial_warnings,
                evidence_fingerprint=decision.evidence_fingerprint,
            )
            session.add(record)
            session.commit()
            return record

    def latest_ethics(self, ticker: str) -> EthicalEvaluation | None:
        with Session(self.engine, expire_on_commit=False) as session:
            return session.scalar(
                select(EthicalEvaluation)
                .where(EthicalEvaluation.ticker == ticker)
                .order_by(
                    EthicalEvaluation.evaluated_at.desc(), EthicalEvaluation.id.desc()
                )
            )

    def save_themes(self, ticker: str, themes: list[ThemeEvidence]) -> None:
        with Session(self.engine) as session:
            for theme in themes:
                exists = session.scalar(
                    select(BusinessTheme.id).where(
                        BusinessTheme.ticker == ticker,
                        BusinessTheme.theme == theme.theme,
                        BusinessTheme.source == theme.source,
                    )
                )
                if exists is None:
                    session.add(BusinessTheme(ticker=ticker, **theme.model_dump()))
            session.commit()

    def save_ai(self, ticker: str, result: AIResearchResult) -> AIResearchAnalysis:
        with Session(self.engine, expire_on_commit=False) as session:
            record = AIResearchAnalysis(
                ticker=ticker,
                source_document_ids=[item.document_id for item in result.evidence],
                component_scores={
                    key: value
                    for key, value in result.model_dump().items()
                    if key.endswith("_score")
                },
                key_positives=result.key_positives,
                key_risks=result.key_risks,
                evidence=[item.model_dump() for item in result.evidence],
                provider=result.provider,
                model=result.model,
                prompt_version=result.prompt_version,
                raw_output=result.raw_structured_output(),
                ai_rating=result.ai_rating,
                confidence=result.confidence,
                analysis_date=result.analysis_date,
            )
            session.add(record)
            session.commit()
            return record

    def save_screener(self, name: str, criteria: dict) -> SavedScreener:
        with Session(self.engine, expire_on_commit=False) as session:
            record = session.scalar(
                select(SavedScreener).where(SavedScreener.name == name)
            )
            if record is None:
                record = SavedScreener(name=name, criteria=criteria)
                session.add(record)
            else:
                record.criteria = criteria
                record.updated_at = datetime.now(UTC)
            session.commit()
            return record

    def list_screeners(self) -> list[SavedScreener]:
        with Session(self.engine, expire_on_commit=False) as session:
            return list(
                session.scalars(select(SavedScreener).order_by(SavedScreener.name))
            )

    def rename_screener(self, old: str, new: str) -> None:
        with Session(self.engine) as session:
            record = session.scalar(
                select(SavedScreener).where(SavedScreener.name == old)
            )
            if record is None:
                raise KeyError(old)
            record.name = new
            record.updated_at = datetime.now(UTC)
            session.commit()

    def delete_screener(self, name: str) -> None:
        with Session(self.engine) as session:
            session.execute(delete(SavedScreener).where(SavedScreener.name == name))
            session.commit()
