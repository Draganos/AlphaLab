"""Relational audit store. Nullable fields mean unavailable, never fabricated."""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Security(Base):
    __tablename__ = "securities"
    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str | None] = mapped_column(String(8))
    asset_type: Mapped[str | None] = mapped_column(String(64))
    industry: Mapped[str | None] = mapped_column(String(128))
    market_cap: Mapped[float | None] = mapped_column(Float)
    business_description: Mapped[str | None] = mapped_column(Text)
    metadata_provider: Mapped[str | None] = mapped_column(String(64))
    metadata_source: Mapped[str | None] = mapped_column(String(512))
    metadata_updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("ticker", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adjusted_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown"
    )
    source: Mapped[str | None] = mapped_column(String(512))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Fundamental(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("observation_hash", name="uq_fundamentals_observation_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    period: Mapped[date] = mapped_column(Date)
    publication_date: Mapped[date | None] = mapped_column(Date, index=True)
    revenue: Mapped[float | None] = mapped_column(Float)
    ebitda: Mapped[float | None] = mapped_column(Float)
    ebit: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    total_equity: Mapped[float | None] = mapped_column(Float)
    shares_outstanding: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown"
    )
    source: Mapped[str | None] = mapped_column(String(512))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    observation_hash: Mapped[str] = mapped_column(String(64), index=True)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    total_assets: Mapped[float | None] = mapped_column(Float)
    current_assets: Mapped[float | None] = mapped_column(Float)
    current_liabilities: Mapped[float | None] = mapped_column(Float)
    interest_expense: Mapped[float | None] = mapped_column(Float)
    dividends_paid: Mapped[float | None] = mapped_column(Float)
    share_repurchases: Mapped[float | None] = mapped_column(Float)
    provenance_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Estimate(Base):
    __tablename__ = "estimates"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    observation_date: Mapped[date] = mapped_column(Date, index=True)
    fiscal_period: Mapped[date] = mapped_column(Date)
    consensus_eps: Mapped[float | None] = mapped_column(Float)
    consensus_revenue: Mapped[float | None] = mapped_column(Float)
    analyst_count: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown"
    )
    source: Mapped[str | None] = mapped_column(String(512))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    estimate_dispersion: Mapped[float | None] = mapped_column(Float)
    observation_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )


class CompanyDocument(Base):
    __tablename__ = "company_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    document_date: Mapped[date] = mapped_column(Date)
    document_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(1024))
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class AIAnalysis(Base):
    __tablename__ = "ai_analysis"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("company_documents.id"))
    guidance_score: Mapped[float | None] = mapped_column(Float)
    demand_score: Mapped[float | None] = mapped_column(Float)
    margin_score: Mapped[float | None] = mapped_column(Float)
    balance_sheet_score: Mapped[float | None] = mapped_column(Float)
    management_confidence: Mapped[float | None] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    raw_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    analysis_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FactorScore(Base):
    __tablename__ = "factor_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    factor_name: Mapped[str] = mapped_column(String(64))
    raw_value: Mapped[float | None] = mapped_column(Float)
    percentile_rank: Mapped[float | None] = mapped_column(Float)
    normalized_score: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    score_version: Mapped[str] = mapped_column(String(32), server_default="legacy")
    config_hash: Mapped[str] = mapped_column(
        String(64), index=True, server_default="legacy"
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    portfolio_name: Mapped[str] = mapped_column(String(128))
    cash: Mapped[float] = mapped_column(Float)
    holdings: Mapped[dict[str, Any]] = mapped_column(JSON)


class SimulatedTrade(Base):
    __tablename__ = "simulated_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ticker: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    transaction_cost: Mapped[float] = mapped_column(Float, default=0)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class EthicalEvaluation(Base):
    __tablename__ = "ethical_evaluations"
    __table_args__ = (UniqueConstraint("ticker", "evaluated_at", "policy_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    ethical_status: Mapped[str] = mapped_column(String(16), index=True)
    primary_business: Mapped[str | None] = mapped_column(String(255))
    business_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclusion_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source: Mapped[str | None] = mapped_column(String(512))
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )
    policy_version: Mapped[str] = mapped_column(String(64), index=True)
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_override_reason: Mapped[str | None] = mapped_column(Text)
    financial_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)


class BusinessTheme(Base):
    __tablename__ = "business_themes"
    __table_args__ = (UniqueConstraint("ticker", "theme", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    theme: Mapped[str] = mapped_column(String(128), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(512))
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class SavedScreener(Base):
    __tablename__ = "saved_screeners"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class AIResearchAnalysis(Base):
    __tablename__ = "ai_research_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    source_document_ids: Mapped[list[int]] = mapped_column(JSON)
    analyzed_document_ids: Mapped[list[int] | None] = mapped_column(JSON)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    component_scores: Mapped[dict[str, float]] = mapped_column(JSON)
    key_positives: Mapped[list[str]] = mapped_column(JSON, default=list)
    key_risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSON)
    ai_rating: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    analysis_date: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class SECCompanyFact(Base):
    """Append-only XBRL fact with SEC knowledge-time and concept provenance."""

    __tablename__ = "sec_company_facts"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "accession", "concept", "unit", "period_start", "period_end"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    cik: Mapped[str] = mapped_column(String(10), index=True)
    taxonomy: Mapped[str] = mapped_column(String(32))
    concept: Mapped[str] = mapped_column(String(255), index=True)
    metric: Mapped[str | None] = mapped_column(String(64), index=True)
    unit: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    filed_date: Mapped[date] = mapped_column(Date, index=True)
    form: Mapped[str] = mapped_column(String(16))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(8))
    accession: Mapped[str] = mapped_column(String(32), index=True)
    frame: Mapped[str | None] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(1024))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class CurrentResearchBuild(Base):
    """Audit header for an explicitly triggered current-only research rebuild."""

    __tablename__ = "current_research_builds"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_date: Mapped[date] = mapped_column(Date, index=True)
    built_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )
    score_version: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    security_count: Mapped[int] = mapped_column(Integer)


class CurrentResearchSnapshot(Base):
    """Persisted live/current state; historical services never query this table."""

    __tablename__ = "current_research_snapshots"
    __table_args__ = (UniqueConstraint("build_id", "ticker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(
        ForeignKey("current_research_builds.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ResearchSnapshot(Base):
    """Immutable, append-only historical alpha_lab.research.StockResearch record.

    Never updated in place — see alpha_lab.research.snapshots.ResearchSnapshotRepository,
    which exposes only save/get/list operations, no update. A row is a frozen
    point-in-time research state: `payload` is the exact StockResearch JSON as
    it existed at `evaluation_date`/`generated_at`, and must never be
    rebuilt from current provider data on read. Separate from
    CurrentResearchSnapshot/CurrentResearchBuild, which persist the
    pre-canonical LiveResearchRecord for the live screener and are owned by
    alpha_lab.screener; this table is owned by alpha_lab.research and stores
    the canonical, evidence-first object instead.
    """

    __tablename__ = "research_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Deterministic content identity: sha256 of {ticker, evaluation_date,
    # rating_version, configuration_hash, payload_hash} — see
    # alpha_lab.research.snapshots._snapshot_id. Deliberately excludes
    # generated_at so re-persisting identical research seconds apart is
    # idempotent rather than creating a duplicate row.
    snapshot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    evaluation_date: Mapped[date] = mapped_column(Date, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime)
    rating_version: Mapped[str] = mapped_column(String(64), index=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    research_schema_version: Mapped[str] = mapped_column(String(32), index=True)
    overall_score: Mapped[float | None] = mapped_column(Float)
    overall_coverage: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(32))
    data_quality_status: Mapped[str] = mapped_column(String(32))
    # Deterministic hash of the canonical StockResearch payload (excluding
    # generated_at) — answers "has the persisted research actually changed?",
    # not a cryptographic authentication of the row.
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class CurrentAnalystConsensus(Base):
    """Current (not historical) Analyst Consensus, one row per ticker.

    Upserted by an explicit refresh (see
    alpha_lab.research.supplemental_service.SupplementalResearchService);
    never written by a read path. A failed refresh leaves this row
    untouched -- the last successfully computed consensus is never erased
    by a provider failure. `payload` is the full serialized
    alpha_lab.research.analyst_consensus.AnalystConsensus.
    """

    __tablename__ = "current_analyst_consensus"
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class CurrentTechnicalSummary(Base):
    """Current Technical Summary, one row per ticker. See
    CurrentAnalystConsensus's docstring for the upsert/failure semantics;
    `payload` is the full serialized
    alpha_lab.research.technical.TechnicalSummary.
    """

    __tablename__ = "current_technical_summary"
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class CurrentAIResearchAssessment(Base):
    """Current AI Research Rating, one row per ticker. See
    CurrentAnalystConsensus's docstring for the upsert/failure semantics;
    `payload` is the full serialized
    alpha_lab.research.ai_rating.AIResearchAssessment.
    """

    __tablename__ = "current_ai_research_assessments"
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
