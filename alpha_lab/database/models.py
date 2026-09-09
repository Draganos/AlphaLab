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


class CurrentExternalCalibration(Base):
    """Current (not historical) external calibration, one row per source
    (e.g. "Donatien"). Upserted only by an explicit refresh (see
    alpha_lab.calibration.service.ExternalCalibrationService); never written
    by a read path. A failed refresh leaves this row untouched -- the last
    successfully validated calibration is never erased by a provider
    failure or a schema-validation failure.

    This is EXTERNAL_CALIBRATION, not ground truth and not a stock-rating
    input -- nothing in alpha_lab.research/screener/strategy reads this
    table. `source_run_time_raw` is kept verbatim (no seconds/timezone
    exist in the source); `source_observed_at` is a best-effort naive
    convenience combination of the source's own date+time, never a
    timezone-attached or AlphaLab-retrieval-time substitute.
    """

    __tablename__ = "current_external_calibration"
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_run_date: Mapped[date | None] = mapped_column(Date)
    source_run_time_raw: Mapped[str | None] = mapped_column(String(16))
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    supersedes: Mapped[str | None] = mapped_column(String(255))
    schema_version: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(1024))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class ExternalCalibrationSnapshot(Base):
    """Immutable, append-only historical external calibration observation.

    Mirrors alpha_lab.research.snapshots.ResearchSnapshot's identity/hash
    pattern: `snapshot_id` is a deterministic sha256 of {source,
    content_hash} so re-persisting an unchanged observation is idempotent
    rather than creating a duplicate row. Unlike ResearchSnapshot, no field
    is excluded from `content_hash` -- every field in the raw Donatien
    payload is substantive source content, not an AlphaLab-added volatile
    timestamp.
    """

    __tablename__ = "external_calibration_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_run_date: Mapped[date | None] = mapped_column(Date)
    source_run_time_raw: Mapped[str | None] = mapped_column(String(16))
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    supersedes: Mapped[str | None] = mapped_column(String(255))
    schema_version: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(1024))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class CurrentMacroAssessment(Base):
    """Current (not historical) AlphaLab Macro Regime read, one row per
    `scope` (e.g. "US"). Upserted only by an explicit refresh (see
    alpha_lab.macro.service.MacroRegimeService); never written by a read
    path. A failed refresh (e.g. ingestion failure for a proxy ticker)
    leaves this row untouched.

    Deterministic, market-derived-proxy only -- never official economic
    data, and never a scoring input. Nothing in alpha_lab.research/
    screener/strategy/backtest/portfolio reads this table.
    """

    __tablename__ = "current_macro_assessment"
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    regime: Mapped[str] = mapped_column(String(16))
    regime_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    coverage: Mapped[float] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    methodology_version: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[date] = mapped_column(Date)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class MacroAssessmentSnapshot(Base):
    """Immutable, append-only historical AlphaLab Macro Regime observation.

    Mirrors ExternalCalibrationSnapshot's identity/hash pattern:
    `snapshot_id` is a deterministic sha256 of {scope, content_hash}, so
    re-persisting an unchanged assessment is idempotent rather than
    creating a duplicate row.
    """

    __tablename__ = "macro_assessment_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    regime: Mapped[str] = mapped_column(String(16))
    regime_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    coverage: Mapped[float] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    methodology_version: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[date] = mapped_column(Date)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class CurrentAlignmentAssessment(Base):
    """Current (not historical) Donatien <-> Market Regime alignment read,
    one row per `scope` (mirrors alpha_lab.macro's scope, e.g. "US").
    Upserted only by an explicit refresh (see
    alpha_lab.alignment.service.AlignmentService); never written by a read
    path. A failed refresh (either upstream source unavailable) leaves this
    row untouched.

    Purely categorical: `alignment` is one of
    ALIGNED/CONFLICT/NEUTRAL/INSUFFICIENT_DATA -- there is no
    alignment_score/conviction_score anywhere in this table. Nothing in
    alpha_lab.research/screener/strategy/backtest/portfolio reads this
    table; see alpha_lab.alignment.alignment's module docstring.
    """

    __tablename__ = "current_alignment_assessment"
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date)
    alignment: Mapped[str] = mapped_column(String(24))
    methodology_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class AlignmentAssessmentSnapshot(Base):
    """Immutable, append-only historical alignment observation. Mirrors
    MacroAssessmentSnapshot/ExternalCalibrationSnapshot's identity/hash
    pattern: `snapshot_id` is a deterministic sha256 of {scope,
    content_hash}, so re-persisting an unchanged alignment read is
    idempotent rather than creating a duplicate row.
    """

    __tablename__ = "alignment_assessment_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    alignment: Mapped[str] = mapped_column(String(24))
    methodology_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
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
