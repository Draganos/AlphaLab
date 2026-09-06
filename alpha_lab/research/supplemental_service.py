"""Persistence/orchestration for Analyst Consensus, Technical Summary, and
AI Research Rating -- the three new research domains.

Deliberately separate from ``alpha_lab.screener.service``/
``alpha_lab.research.service`` (the existing fundamental-score pipeline):
this module never touches ``LiveResearchRecord``, ``category_scores``, or
``StockResearch.overall_score``. Refreshing these three domains never
changes the fundamental score.

Read methods here are pure database reads (no provider, no computation) --
safe to call from a Streamlit render. Refresh methods call a provider
and/or run indicator/AI computation and are meant to be triggered
explicitly (a script or a UI button), never from an ordinary page render.

A failed refresh for one ticker/domain leaves that domain's existing
`Current*` row untouched: the provider call (or, for AI, evidence
preparation) happens before any database write, exactly mirroring
``alpha_lab.ingestion.service.IngestionService``'s existing safety pattern.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import (
    CurrentAIResearchAssessment,
    CurrentAnalystConsensus,
    CurrentTechnicalSummary,
    Price,
)
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError
from alpha_lab.research.ai_rating import (
    AIResearchAssessment,
    build_ai_research_assessment,
    build_evidence_coverage,
    build_evidence_payload,
    configured_ai_rating_provider,
)
from alpha_lab.research.analyst_consensus import AnalystConsensus, build_analyst_consensus
from alpha_lab.research.model import StockResearch
from alpha_lab.research.technical import TechnicalSummary, build_technical_summary


@dataclass
class SupplementalRefreshResult:
    """Outcome of `SupplementalResearchService.refresh_all` -- one explicit
    three-domain refresh. `analyst_error` is set (and `ai_research_assessment`
    left `None`) exactly when Analyst Consensus failed to refresh; see
    `refresh_all` for why the AI assessment is skipped rather than degraded
    in that case."""

    analyst_consensus: AnalystConsensus | None
    technical_summary: TechnicalSummary
    ai_research_assessment: AIResearchAssessment | None
    analyst_error: ProviderError | None


class SupplementalResearchService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation ------------------------

    def get_analyst_consensus(self, ticker: str) -> AnalystConsensus | None:
        row = self._get_row(CurrentAnalystConsensus, ticker)
        return None if row is None else AnalystConsensus.model_validate(row.payload)

    def get_technical_summary(self, ticker: str) -> TechnicalSummary | None:
        row = self._get_row(CurrentTechnicalSummary, ticker)
        return None if row is None else TechnicalSummary.model_validate(row.payload)

    def get_ai_research_assessment(self, ticker: str) -> AIResearchAssessment | None:
        row = self._get_row(CurrentAIResearchAssessment, ticker)
        return None if row is None else AIResearchAssessment.model_validate(row.payload)

    def _get_row(self, model, ticker: str):
        normalized = ticker.strip().upper()
        with Session(self.engine) as session:
            return session.get(model, normalized)

    # --- refreshes: explicit, provider calls happen before any DB write ---

    def refresh_analyst_consensus(
        self, ticker: str, provider: MarketDataProvider
    ) -> AnalystConsensus:
        """Fetch + compute + upsert. Raises ProviderError on failure,
        leaving the existing row (if any) untouched."""
        symbol = ticker.strip().upper()
        raw = provider.get_analyst_consensus(symbol)
        consensus = build_analyst_consensus(
            ticker=symbol,
            strong_buy=raw.get("strong_buy"),
            buy=raw.get("buy"),
            hold=raw.get("hold"),
            sell=raw.get("sell"),
            strong_sell=raw.get("strong_sell"),
            target_current=raw.get("target_current"),
            target_low=raw.get("target_low"),
            target_mean=raw.get("target_mean"),
            target_median=raw.get("target_median"),
            target_high=raw.get("target_high"),
            as_of=raw.get("as_of"),
            source=raw.get("source", provider.provider_name),
        )
        self._upsert(CurrentAnalystConsensus, symbol, consensus.model_dump(mode="json"))
        return consensus

    def refresh_technical_summary(
        self, ticker: str, *, as_of: date | None = None
    ) -> TechnicalSummary:
        """Compute from AlphaLab's own stored Price history -- no network.
        Always succeeds (an empty/short price history yields a REVIEW
        summary with zero coverage, which is honest, not an error)."""
        symbol = ticker.strip().upper()
        with Session(self.engine) as session:
            rows = session.scalars(
                select(Price).where(Price.ticker == symbol).order_by(Price.date)
            ).all()
        frame = pd.DataFrame(
            [
                {"date": row.date, "close": row.close, "high": row.high, "low": row.low}
                for row in rows
                if row.close is not None
            ]
        )
        if not frame.empty:
            frame = frame.set_index("date")
        summary = build_technical_summary(
            symbol,
            frame,
            as_of=as_of or date.today(),
            source="AlphaLabPriceHistory",
        )
        self._upsert(CurrentTechnicalSummary, symbol, summary.model_dump(mode="json"))
        return summary

    def refresh_ai_research_assessment(
        self,
        ticker: str,
        research: StockResearch,
        *,
        analyst_consensus: AnalystConsensus | None = None,
        technical_summary: TechnicalSummary | None = None,
    ) -> AIResearchAssessment:
        """Synthesize already-computed evidence. `research` must be the
        base StockResearch (fundamental evidence only); pass the current
        analyst_consensus/technical_summary explicitly so this never has to
        read them back itself. Raises EvidenceViolation if the configured
        provider cites evidence outside what was supplied -- never silently
        corrected."""
        symbol = ticker.strip().upper()
        evidence = build_evidence_payload(
            categories=research.categories,
            analyst_consensus=analyst_consensus,
            technical_summary=technical_summary,
        )
        # Domain-aware: a missing Analyst Consensus or Technical Summary
        # counts as 0 coverage for that domain, never as "not applicable"
        # and excluded from the average -- see AIEvidenceCoverage.
        evidence_coverage = build_evidence_coverage(
            fundamental_coverage=research.overall_coverage,
            analyst_consensus=analyst_consensus,
            technical_summary=technical_summary,
        )

        provider = configured_ai_rating_provider()
        raw = provider.assess(symbol, evidence)
        assessment = build_ai_research_assessment(
            ticker=symbol,
            raw=raw,
            evidence=evidence,
            evidence_coverage=evidence_coverage,
            research_schema_version="stockresearch-v2",
            as_of=research.evaluation_date,
            generated_at=datetime.now(UTC),
        )
        self._upsert(CurrentAIResearchAssessment, symbol, assessment.model_dump(mode="json"))
        return assessment

    def refresh_all(
        self, ticker: str, provider: MarketDataProvider, research: StockResearch
    ) -> SupplementalRefreshResult:
        """The explicit "Refresh for this ticker" action's full sequence:
        Analyst Consensus, then Technical Summary (independent of Analyst
        Consensus, so always attempted), then AI Research Rating -- but only
        when Analyst Consensus refreshed cleanly.

        A failed Analyst Consensus refresh never triggers an AI refresh: the
        AI Research Rating explicitly synthesizes all three domains, and
        synthesizing it anyway with a missing Analyst Consensus would
        silently replace a previously valid assessment with a weaker one
        derived from incomplete evidence, rather than surfacing the failure.
        The existing AI Research Rating (if any) is left exactly as it was
        when Analyst Consensus fails.
        """
        analyst: AnalystConsensus | None = None
        analyst_error: ProviderError | None = None
        try:
            analyst = self.refresh_analyst_consensus(ticker, provider)
        except ProviderError as error:
            analyst_error = error
        technical = self.refresh_technical_summary(ticker)
        ai_assessment: AIResearchAssessment | None = None
        if analyst_error is None:
            ai_assessment = self.refresh_ai_research_assessment(
                ticker, research, analyst_consensus=analyst, technical_summary=technical
            )
        return SupplementalRefreshResult(
            analyst_consensus=analyst,
            technical_summary=technical,
            ai_research_assessment=ai_assessment,
            analyst_error=analyst_error,
        )

    def _upsert(self, model, ticker: str, payload: dict) -> None:
        with Session(self.engine) as session:
            row = session.get(model, ticker)
            if row is None:
                session.add(model(ticker=ticker, payload=payload))
            else:
                row.payload = payload
                row.computed_at = datetime.now(UTC)
            session.commit()
