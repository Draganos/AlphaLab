"""Canonical Research State (roadmap Phase 5): a single, per-security,
point-in-time assembler over AlphaLab's existing evidence domains.

`get_research_state(engine, settings, ticker, evaluation_date=None)` is the
one function a consumer -- the dashboard, the Company Research page, a
future AI Research step, a future agent -- calls instead of independently
reconstructing `StockResearch` + News + Macro + Donatien + Alignment +
Ethics + a deterministic score, each from its own page-specific code path.
Pages are views; this is the source of truth they read from.

**Pure assembler, not a new persisted table.** Every field is either a
direct reference to an already-persisted domain object or a thin read
through an already-existing (or, for Alignment/Ethics, newly-added in this
same phase) point-in-time-safe getter. Nothing here calls a provider, and
nothing here writes to the database -- see each field's construction
below. This mirrors `alpha_lab.evidence_coverage.build_security_coverage_
summary`'s own existing pattern exactly, just widened to every evidence
domain instead of only the ones `StockResearch`/News/Macro already reach.

**Two read paths for `StockResearch`-derived fields** (fundamentals,
analyst_activity, technicals, ai_rating), chosen by whether `evaluation_
date` is today or in the past:
  - today (or omitted): `ResearchService.get_stock_research` -- the live
    current-research path, guaranteed to exist for any tracked ticker with
    a current research build, exactly what the dashboard/screener already
    read.
  - a past date: `ResearchSnapshotRepository.get_latest_as_of` -- the
    Historical Research Reconstruction mechanism's own PIT-safe read.
    Honestly returns `None` (NOT_COMPUTED) if no snapshot was ever
    persisted for that ticker by that date -- confirmed against the real
    database that not every tracked ticker has one (a ticker added but
    never explicitly refreshed via `scripts/manage_universe.py add`/the
    Company Research "Refresh for this ticker" button has none), so this
    is a genuine, expected gap, not a bug to paper over.
This two-path split is a deliberate, documented exception to "one PIT-safe
code path, no separate current shortcut" (the principle `AlignmentService.
refresh` otherwise establishes): unlike Alignment/Macro, StockResearch's
historical state is not cheaply recomputable on demand from raw evidence
-- it depends on a snapshot having actually been taken.

**`ResearchField.status`/`.observed_at` are deliberately independent, never
collapsed into one derived figure.** `status` (`alpha_lab.evidence_coverage.
CoverageStatus`'s own vocabulary) answers "is this field's evidence
available at all"; `observed_at` answers "how fresh is it." A field can be
FULL and stale (available, but its `observed_at` is old), or NOT_COMPUTED
regardless of any freshness question (there is nothing to be fresh or
stale about). Locked into the contract per explicit design review.

**`provenance_id`**: a stable, reproducible identity for the exact
evidence a field's value came from -- the domain's own persisted
snapshot/row id where the read path already returns one (Macro/Donatien/
Alignment snapshot_id, Ethics/AIResearchAnalysis row id), otherwise a
deterministic sha256 over the field's own serialized value, the same
canonical-json-hash idiom already established three times in this
codebase (Donatien/Macro/Alignment snapshot identity, Phase 3's
`research_refresh.py` version_id). Re-assembling identical evidence always
yields the identical provenance_id. This is the addressable handle a
future AI/agent step (roadmap Phase 6+) cites as exactly what it computed
against -- distinct from `research_refresh_version_id`, which identifies
the whole tracked-universe refresh cycle this state was assembled during,
not any one field's own evidence.

**`conflicts`**: reserved for roadmap Phase 8 (the Conviction/Conflict
Layer). Always `None` here -- this phase does not compute it, and never
fabricates a placeholder score in its place.
"""

from dataclasses import asdict
from datetime import date, datetime, time
from typing import Any
import hashlib
import json

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from pydantic import BaseModel

from alpha_lab.alignment import AlignmentAssessment, AlignmentService
from alpha_lab.calibration import ExternalCalibrationService
from alpha_lab.config import Settings
from alpha_lab.database.models import AIResearchAnalysis
from alpha_lab.ethics import EthicalClassificationService, load_ethics_policy
from alpha_lab.evidence_coverage import build_security_coverage_summary, SecurityCoverageSummary
from alpha_lab.macro import MacroAssessment, MacroRegimeService
from alpha_lab.news import NewsService
from alpha_lab.news.impact import NewsImpact, classify_article
from alpha_lab.providers.donatien import DonatienCalibration
from alpha_lab.research import ResearchService
from alpha_lab.research.snapshots import ResearchSnapshotRepository
from alpha_lab.research.model import StockResearch
from alpha_lab.research_refresh import get_current_research_refresh_status
from alpha_lab.strategy import HistoricalScoringService

RESEARCH_STATE_METHODOLOGY_VERSION = "research-state-v1"


class ResearchField(BaseModel):
    """One evidence domain's value + availability + freshness + identity,
    for one security at one evaluation moment. See this module's own
    docstring for why `status`/`observed_at` stay independent and what
    `provenance_id` means."""

    value: Any = None
    source: str | None = None
    observed_at: str | None = None
    status: str
    provenance_id: str | None = None
    detail: str | None = None


class ResearchState(BaseModel):
    """`get_research_state`'s own output -- the one object a dashboard
    page, the screener, or a future AI/agent step reads instead of
    independently reconstructing each domain."""

    ticker: str
    evaluation_date: date
    research_refresh_version_id: str | None = None

    # The exact StockResearch object the fundamentals/analyst_activity/
    # technicals/ai_rating fields below were derived from -- exposed
    # directly (not only decomposed) so an existing consumer that already
    # works with a full StockResearch (e.g. the Company Research page) can
    # switch to this assembler as a drop-in replacement for `ResearchService
    # .get_stock_research(ticker)`/`ResearchSnapshotRepository.get_latest_
    # as_of(ticker, as_of)` without losing any of that object's own
    # structure. `None` under the exact same conditions those two calls
    # would have returned `None`.
    stock_research: StockResearch | None = None

    fundamentals: ResearchField
    analyst_activity: ResearchField
    technicals: ResearchField
    news: ResearchField
    news_impact: ResearchField
    macro: ResearchField
    donatien: ResearchField
    donatien_alignment: ResearchField
    ethics: ResearchField
    deterministic_score: ResearchField
    ai_research: ResearchField
    ai_rating: ResearchField
    coverage: SecurityCoverageSummary | None = None
    conflicts: list[dict] | None = None
    methodology_version: str = RESEARCH_STATE_METHODOLOGY_VERSION


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _computed_provenance_id(domain: str, ticker: str, evaluation_date: date, value: object) -> str:
    identity = {"domain": domain, "ticker": ticker, "evaluation_date": evaluation_date.isoformat(), "value": value}
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _status_for(coverage: float | None) -> str:
    if coverage is None:
        return "NOT_COMPUTED"
    if coverage >= 1.0:
        return "FULL"
    if coverage <= 0.0:
        return "NO_EVIDENCE"
    return "PARTIAL"


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _upper_bound(as_of: date) -> datetime:
    return datetime.combine(as_of, time.max)


def get_research_state(
    engine: Engine, settings: Settings, ticker: str, evaluation_date: date | None = None,
) -> ResearchState:
    """Zero network calls, zero writes -- every read below is either
    already-established as point-in-time-safe, or genuinely read-only
    (see this module's own docstring for the two exceptions added in this
    phase: `AlignmentService.get_assessment_as_of`, `EthicalClassification
    Service.get_evaluation_as_of`)."""
    ticker = ticker.strip().upper()
    today = date.today()
    evaluation_date = evaluation_date or today
    is_today = evaluation_date >= today

    research_service = ResearchService(engine, settings)
    if is_today:
        research = research_service.get_stock_research(ticker)
    else:
        research = ResearchSnapshotRepository(engine).get_latest_as_of(ticker, evaluation_date)

    news_service = NewsService(engine)
    articles = news_service.get_history(ticker, as_of=evaluation_date)

    macro_snapshot = MacroRegimeService(engine).get_assessment_as_of(as_of=evaluation_date)
    macro_assessment = None if macro_snapshot is None else MacroAssessment.model_validate(macro_snapshot.payload)

    donatien_snapshot = ExternalCalibrationService(engine).get_calibration_as_of(as_of=evaluation_date)
    donatien_calibration = (
        None if donatien_snapshot is None
        else DonatienCalibration.model_validate(donatien_snapshot.normalized_payload)
    )

    alignment_snapshot = AlignmentService(engine).get_assessment_as_of(as_of=evaluation_date)
    alignment_assessment = (
        None if alignment_snapshot is None else AlignmentAssessment.model_validate(alignment_snapshot.payload)
    )

    ethics_policy = load_ethics_policy(settings.ethics_policy_path)
    ethics_row = EthicalClassificationService(engine, ethics_policy).get_evaluation_as_of(
        ticker, as_of=evaluation_date
    )

    scores = HistoricalScoringService(engine, settings).score_universe_as_of(
        evaluation_date, tickers=[ticker], min_score=0, minimum_coverage=0
    )
    score = scores[0] if scores else None

    with Session(engine) as session:
        ai_analysis = session.scalar(
            select(AIResearchAnalysis)
            .where(AIResearchAnalysis.ticker == ticker, AIResearchAnalysis.analysis_date <= _upper_bound(evaluation_date))
            .order_by(AIResearchAnalysis.analysis_date.desc(), AIResearchAnalysis.id.desc())
        )
        if ai_analysis is not None:
            session.expunge(ai_analysis)

    coverage = (
        build_security_coverage_summary(research, news_articles=articles, macro_assessment=macro_assessment)
        if research is not None
        else None
    )

    refresh_status = get_current_research_refresh_status(engine)

    return ResearchState(
        ticker=ticker,
        evaluation_date=evaluation_date,
        research_refresh_version_id=None if refresh_status is None else refresh_status.version_id,
        stock_research=research,
        fundamentals=_fundamentals_field(ticker, evaluation_date, research),
        analyst_activity=_analyst_activity_field(ticker, evaluation_date, research),
        technicals=_technicals_field(ticker, evaluation_date, research),
        news=_news_field(ticker, evaluation_date, articles),
        news_impact=_news_impact_field(ticker, evaluation_date, articles),
        macro=_macro_field(macro_snapshot, macro_assessment),
        donatien=_donatien_field(donatien_snapshot, donatien_calibration),
        donatien_alignment=_alignment_field(alignment_snapshot, alignment_assessment),
        ethics=_ethics_field(ethics_row),
        deterministic_score=_score_field(ticker, evaluation_date, score),
        ai_research=_ai_research_field(ai_analysis),
        ai_rating=_ai_rating_field(ticker, evaluation_date, research),
        coverage=coverage,
    )


def _fundamentals_field(ticker: str, evaluation_date: date, research: StockResearch | None) -> ResearchField:
    if research is None:
        return ResearchField(status="NOT_COMPUTED")
    sources = sorted({s for category in research.categories.values() for s in category.sources})
    return ResearchField(
        value={name: category.model_dump(mode="json") for name, category in research.categories.items()},
        source=", ".join(sources) if sources else None,
        observed_at=_iso(research.generated_at),
        status=_status_for(research.overall_coverage),
        provenance_id=_computed_provenance_id(
            "fundamentals", ticker, evaluation_date,
            {name: category.model_dump(mode="json") for name, category in research.categories.items()},
        ),
        detail=f"overall_coverage={research.overall_coverage:.0%}, confidence={research.confidence}",
    )


def _analyst_activity_field(ticker: str, evaluation_date: date, research: StockResearch | None) -> ResearchField:
    consensus = None if research is None else research.analyst_consensus
    history = None if research is None else research.analyst_research
    if consensus is None and history is None:
        return ResearchField(status="NOT_COMPUTED")
    value = {
        "consensus": None if consensus is None else consensus.model_dump(mode="json"),
        "history": None if history is None else history.model_dump(mode="json"),
    }
    return ResearchField(
        value=value,
        source=None if consensus is None else consensus.source,
        observed_at=_iso(consensus.as_of) if consensus is not None else None,
        status=_status_for(None if consensus is None else consensus.coverage),
        provenance_id=_computed_provenance_id("analyst_activity", ticker, evaluation_date, value),
    )


def _technicals_field(ticker: str, evaluation_date: date, research: StockResearch | None) -> ResearchField:
    technical = None if research is None else research.technical_summary
    if technical is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value=technical.model_dump(mode="json"),
        source=technical.source,
        observed_at=_iso(technical.as_of),
        status=_status_for(technical.coverage),
        provenance_id=_computed_provenance_id(
            "technicals", ticker, evaluation_date, technical.model_dump(mode="json")
        ),
    )


def _news_field(ticker: str, evaluation_date: date, articles: list) -> ResearchField:
    if not articles:
        return ResearchField(status="NO_EVIDENCE", value=[])
    content_hashes = sorted(article.content_hash for article in articles)
    return ResearchField(
        value=[article.content_hash for article in articles],
        source=", ".join(sorted({article.provider for article in articles})),
        observed_at=_iso(max(article.published_at for article in articles)),
        status="FULL",
        provenance_id=_computed_provenance_id("news", ticker, evaluation_date, content_hashes),
        detail=f"{len(articles)} article(s)",
    )


def _news_impact_field(ticker: str, evaluation_date: date, articles: list) -> ResearchField:
    if not articles:
        return ResearchField(status="NOT_COMPUTED")
    impacts: list[NewsImpact] = [classify_article(article) for article in articles]
    classified = [impact for impact in impacts if impact.is_classified]
    value = [impact.model_dump(mode="json") for impact in impacts]
    return ResearchField(
        value=value,
        observed_at=_iso(max(article.published_at for article in articles)),
        status="FULL" if classified else "NO_EVIDENCE",
        provenance_id=_computed_provenance_id("news_impact", ticker, evaluation_date, value),
        detail=f"{len(classified)}/{len(impacts)} article(s) classified",
    )


def _macro_field(macro_snapshot, macro_assessment: MacroAssessment | None) -> ResearchField:
    if macro_snapshot is None or macro_assessment is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value=macro_assessment.model_dump(mode="json"),
        source=macro_assessment.source,
        observed_at=_iso(macro_snapshot.as_of),
        status=_status_for(macro_assessment.coverage),
        provenance_id=macro_snapshot.snapshot_id,
    )


def _donatien_field(donatien_snapshot, donatien_calibration: DonatienCalibration | None) -> ResearchField:
    if donatien_snapshot is None or donatien_calibration is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value=donatien_calibration.model_dump(mode="json"),
        observed_at=_iso(donatien_snapshot.retrieved_at),
        status="FULL",
        provenance_id=donatien_snapshot.snapshot_id,
    )


def _alignment_field(alignment_snapshot, alignment_assessment: AlignmentAssessment | None) -> ResearchField:
    if alignment_snapshot is None or alignment_assessment is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value=alignment_assessment.model_dump(mode="json"),
        observed_at=_iso(alignment_snapshot.as_of),
        status="FULL",
        provenance_id=alignment_snapshot.snapshot_id,
        detail=alignment_assessment.alignment.value,
    )


def _ethics_field(ethics_row) -> ResearchField:
    if ethics_row is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value={"ethical_status": ethics_row.ethical_status, "business_tags": ethics_row.business_tags,
               "exclusion_reasons": ethics_row.exclusion_reasons, "review_reasons": ethics_row.review_reasons},
        source=ethics_row.source,
        observed_at=_iso(ethics_row.evaluated_at),
        status="FULL",
        provenance_id=str(ethics_row.id),
        detail=ethics_row.ethical_status,
    )


def _score_field(ticker: str, evaluation_date: date, score) -> ResearchField:
    if score is None:
        return ResearchField(status="NOT_COMPUTED")
    value = asdict(score)
    return ResearchField(
        value=value,
        observed_at=_iso(score.evaluation_date),
        status=_status_for(score.coverage),
        provenance_id=_computed_provenance_id("deterministic_score", ticker, evaluation_date, value),
        detail=score.exclusion_reason,
    )


def _ai_research_field(ai_analysis) -> ResearchField:
    if ai_analysis is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value={
            "component_scores": ai_analysis.component_scores, "key_positives": ai_analysis.key_positives,
            "key_risks": ai_analysis.key_risks, "ai_rating": ai_analysis.ai_rating,
            "confidence": ai_analysis.confidence,
        },
        source=ai_analysis.provider,
        observed_at=_iso(ai_analysis.analysis_date),
        status="FULL",
        provenance_id=str(ai_analysis.id),
    )


def _ai_rating_field(ticker: str, evaluation_date: date, research: StockResearch | None) -> ResearchField:
    assessment = None if research is None else research.ai_research_assessment
    if assessment is None:
        return ResearchField(status="NOT_COMPUTED")
    return ResearchField(
        value=assessment.model_dump(mode="json"),
        source=assessment.source,
        observed_at=_iso(assessment.as_of),
        status=_status_for(assessment.evidence_coverage.overall_ai_evidence_coverage),
        provenance_id=_computed_provenance_id(
            "ai_rating", ticker, evaluation_date, assessment.model_dump(mode="json")
        ),
    )
