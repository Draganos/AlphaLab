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
    CurrentResearchBuild,
    CurrentResearchSnapshot,
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

    def save_themes(
        self,
        ticker: str,
        themes: list[ThemeEvidence],
        *,
        source: str | None = None,
        source_prefix: str | None = None,
    ) -> None:
        with Session(self.engine) as session:
            derived_source = source or (themes[0].source if themes else None)
            if derived_source is not None:
                current = {theme.theme for theme in themes}
                source_filter = (
                    BusinessTheme.source.startswith(source_prefix)
                    if source_prefix
                    else BusinessTheme.source == derived_source
                )
                statement = delete(BusinessTheme).where(
                    BusinessTheme.ticker == ticker, source_filter
                )
                if current:
                    statement = statement.where(BusinessTheme.theme.not_in(current))
                session.execute(statement)
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

    def save_ai(
        self,
        ticker: str,
        result: AIResearchResult,
        *,
        analyzed_document_ids: list[int] | None = None,
        input_document_fingerprint: str | None = None,
    ) -> AIResearchAnalysis:
        with Session(self.engine, expire_on_commit=False) as session:
            record = AIResearchAnalysis(
                ticker=ticker,
                source_document_ids=[item.document_id for item in result.evidence],
                analyzed_document_ids=analyzed_document_ids,
                input_fingerprint=input_document_fingerprint,
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
                raw_output={
                    **result.raw_structured_output(),
                    "input_document_fingerprint": input_document_fingerprint,
                },
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

    def save_current_research(self, records: list) -> CurrentResearchBuild | None:
        """Persist one immutable current-only build atomically."""
        if not records:
            return None
        first = records[0]
        with Session(self.engine, expire_on_commit=False) as session:
            build = CurrentResearchBuild(
                evaluation_date=first.evaluation_date,
                score_version=first.rating_version,
                config_hash=first.configuration_hash,
                security_count=len(records),
            )
            session.add(build)
            session.flush()
            session.add_all(
                CurrentResearchSnapshot(
                    build_id=build.id,
                    ticker=record.ticker,
                    payload=record.model_dump(mode="json"),
                )
                for record in records
            )
            session.commit()
            return build

    def latest_current_payloads(self) -> tuple[CurrentResearchBuild | None, list[dict]]:
        """Read the latest complete build without invoking providers or writing state."""
        with Session(self.engine, expire_on_commit=False) as session:
            build = session.scalar(
                select(CurrentResearchBuild).order_by(
                    CurrentResearchBuild.built_at.desc(), CurrentResearchBuild.id.desc()
                )
            )
            if build is None:
                return None, []
            payloads = list(
                session.scalars(
                    select(CurrentResearchSnapshot.payload)
                    .where(CurrentResearchSnapshot.build_id == build.id)
                    .order_by(CurrentResearchSnapshot.ticker)
                )
            )
            return build, payloads
