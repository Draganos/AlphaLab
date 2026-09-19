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
    CurrentFundEvidence,
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
from alpha_lab.research.analyst_events import AnalystEventsService
from alpha_lab.research.analyst_research import AnalystResearchSummary
from alpha_lab.research.fund_evidence import FundEvidence, build_fund_evidence
from alpha_lab.research.security_type import normalize_security_type
from alpha_lab.research.model import StockResearch
from alpha_lab.research.technical import TechnicalSummary, build_technical_summary


@dataclass
class SupplementalRefreshResult:
    """Outcome of `SupplementalResearchService.refresh_all` -- one explicit
    refresh across Analyst Consensus, Technical Summary, Fund Evidence, and
    AI Research Rating. `analyst_error` is set (and `ai_research_assessment`
    left `None`) exactly when Analyst Consensus failed to refresh; see
    `refresh_all` for why the AI assessment is skipped rather than degraded
    in that case. `fund_evidence_error` is set when a Fund Evidence refresh
    attempt failed (rate limiting, network, ...) -- `fund_evidence` itself
    still reflects the last successfully stored value in that case (never
    `None` just because *this* refresh attempt failed), and never blocks
    the AI Research Rating the way an Analyst Consensus failure does."""

    analyst_consensus: AnalystConsensus | None
    technical_summary: TechnicalSummary
    fund_evidence: "FundEvidence | None"
    ai_research_assessment: AIResearchAssessment | None
    analyst_error: ProviderError | None
    fund_evidence_error: ProviderError | None


class SupplementalResearchService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._analyst_events = AnalystEventsService(engine)

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

    def get_fund_evidence(self, ticker: str) -> FundEvidence | None:
        row = self._get_row(CurrentFundEvidence, ticker)
        return None if row is None else FundEvidence.model_validate(row.payload)

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

    def refresh_fund_evidence(
        self, ticker: str, provider: MarketDataProvider
    ) -> FundEvidence | None:
        """Fetch + compute + upsert. Returns `None` (leaving any existing
        row untouched) when `ticker` genuinely has no fund data -- e.g. it's
        an equity, not an ETF/fund; `YFinanceProvider.get_fund_data` already
        distinguishes that from a real provider failure and returns `None`
        for it rather than raising. Raises `ProviderError` on an actual
        provider failure, also leaving the existing row untouched."""
        symbol = ticker.strip().upper()
        raw = provider.get_fund_data(symbol)
        if raw is None:
            return None
        evidence = build_fund_evidence(raw)
        if evidence is None:
            return None
        self._upsert(CurrentFundEvidence, symbol, evidence.model_dump(mode="json"))
        return evidence

    def refresh_ai_research_assessment(
        self,
        ticker: str,
        research: StockResearch,
        *,
        analyst_consensus: AnalystConsensus | None = None,
        technical_summary: TechnicalSummary | None = None,
        analyst_research: AnalystResearchSummary | None = None,
        fund_evidence: FundEvidence | None = None,
    ) -> AIResearchAssessment:
        """Synthesize already-computed evidence. `research` must be the
        base StockResearch (fundamental evidence only); pass the current
        analyst_consensus/technical_summary/analyst_research/fund_evidence
        explicitly so this never has to read them back itself. Raises
        EvidenceViolation if the configured provider cites evidence outside
        what was supplied -- never silently corrected."""
        symbol = ticker.strip().upper()
        evidence = build_evidence_payload(
            categories=research.categories,
            analyst_consensus=analyst_consensus,
            technical_summary=technical_summary,
            analyst_research=analyst_research,
            fund_evidence=fund_evidence,
        )
        # Domain-aware: a missing Analyst Consensus or Technical Summary
        # counts as 0 coverage for that domain, never as "not applicable"
        # and excluded from the average -- see AIEvidenceCoverage. For an
        # ETF, `security_type` swaps Analyst Consensus for Fund Evidence in
        # that average instead (see build_evidence_coverage's docstring --
        # "do not reuse equity analyst gates blindly").
        evidence_coverage = build_evidence_coverage(
            fundamental_coverage=research.overall_coverage,
            analyst_consensus=analyst_consensus,
            technical_summary=technical_summary,
            fund_evidence=fund_evidence,
            security_type=normalize_security_type(research.security_type),
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
        Analyst Consensus, Technical Summary, and Fund Evidence (all three
        independent of each other, so all always attempted), then AI
        Research Rating -- but only when Analyst Consensus refreshed
        cleanly.

        A failed Analyst Consensus refresh never triggers an AI refresh: the
        AI Research Rating explicitly synthesizes all evidence domains, and
        synthesizing it anyway with a missing Analyst Consensus would
        silently replace a previously valid assessment with a weaker one
        derived from incomplete evidence, rather than surfacing the failure.
        The existing AI Research Rating (if any) is left exactly as it was
        when Analyst Consensus fails. A failed Fund Evidence refresh is
        different -- see `SupplementalRefreshResult`'s docstring -- and
        never blocks the AI refresh the way an Analyst Consensus failure
        does (Fund Evidence is only ever relevant for an ETF in the first
        place, via `build_evidence_coverage`'s `security_type`).
        """
        analyst: AnalystConsensus | None = None
        analyst_error: ProviderError | None = None
        try:
            analyst = self.refresh_analyst_consensus(ticker, provider)
        except ProviderError as error:
            analyst_error = error
        technical = self.refresh_technical_summary(ticker)
        fund_evidence: FundEvidence | None = None
        fund_evidence_error: ProviderError | None = None
        try:
            fund_evidence = self.refresh_fund_evidence(ticker, provider)
        except ProviderError as error:
            fund_evidence_error = error
            fund_evidence = self.get_fund_evidence(ticker)
        ai_assessment: AIResearchAssessment | None = None
        if analyst_error is None:
            # Pure DB read (no provider call) of whatever Analyst Research
            # evidence (rating changes / revision trend) is already stored
            # -- refreshing that evidence is its own separate explicit
            # action (AnalystEventsService.refresh_all), never triggered
            # implicitly by an AI refresh.
            analyst_research = self._analyst_events.get_research_summary(ticker)
            ai_assessment = self.refresh_ai_research_assessment(
                ticker,
                research,
                analyst_consensus=analyst,
                technical_summary=technical,
                analyst_research=analyst_research,
                fund_evidence=fund_evidence,
            )
        return SupplementalRefreshResult(
            analyst_consensus=analyst,
            technical_summary=technical,
            fund_evidence=fund_evidence,
            ai_research_assessment=ai_assessment,
            analyst_error=analyst_error,
            fund_evidence_error=fund_evidence_error,
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
