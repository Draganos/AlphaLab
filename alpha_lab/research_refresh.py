"""Research Refresh Orchestrator (roadmap Phase 3: the successor to #52's
universe-tracking model).

Establishes AlphaLab's versioned "research state" -- what the tracked
universe's evidence looks like, per domain, at one specific moment --
every time it runs. This is the "refresh orchestrator -> establish
research state -> [future] AI may consume only an explicit research-state
version" flow: this module owns the first two steps. A future AI Research/
Rating consumer (roadmap Phase 6) is expected to cite `version_id` as the
exact research state it computed against; nothing here calls or gates any
AI/scoring path itself.

Scope, deliberately narrow for this phase (see ARCHITECTURE.md's own
section for the full rationale):

- Core market/fundamental data (price/fundamentals) keeps its existing,
  already-established freshness policy (`alpha_lab.refresh`'s
  `stale_price_days`) and its existing auto-refresh-if-stale behavior --
  `run_core_refresh` is called here completely unchanged, same function,
  same tests, same semantics.
- Every other evidence domain (Analyst Consensus/Technical/AI Research
  Rating, Analyst History, Revisions, News, Macro Regime, Donatien
  External Calibration, Alignment) is READ, never refreshed, by this
  orchestrator: a pure-DB-read coverage/freshness snapshot across the
  tracked universe, reusing `alpha_lab.evidence_coverage` (PR #28) --
  the exact same read-model the Evidence & Coverage Dashboard page
  already renders -- rather than a second parallel coverage engine.
  None of these domains has an established staleness policy today (see
  ARCHITECTURE.md), so none is auto-refreshed here; the domain list
  below is deliberately the extension point future phases register a
  policy against, without redesigning this module.
- The result is stamped with a stable `version_id` (deterministic sha256
  over the assembled evidence -- the exact idempotency idiom
  `alpha_lab.calibration.service.ExternalCalibrationService.refresh`
  already established) and persisted as current + immutable snapshot
  history, mirroring `CurrentExternalCalibration`/`ExternalCalibrationSnapshot`.
  Re-running with genuinely unchanged evidence is idempotent: the current
  row's `computed_at` advances, no duplicate snapshot is written.
"""

from datetime import UTC, date, datetime
from typing import Any
from collections import Counter
import hashlib
import json

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel

from alpha_lab.alignment import AlignmentService
from alpha_lab.calibration import ExternalCalibrationService
from alpha_lab.config import Settings
from alpha_lab.database.models import (
    CurrentResearchRefreshStatus,
    ResearchRefreshStatusSnapshot,
)
from alpha_lab.evidence_coverage import (
    build_security_coverage_summary,
    flatten_coverage_rows,
)
from alpha_lab.macro import MacroRegimeService
from alpha_lab.news import NewsService
import alpha_lab.refresh as refresh
from alpha_lab.refresh import CoreRefreshResult
from alpha_lab.research import ResearchService

DEFAULT_SCOPE = "default"
METHODOLOGY_VERSION = "research-refresh-status-v1"


class DomainFreshness(BaseModel):
    """One evidence domain's coverage/freshness across the whole tracked
    universe at this research state's evaluation moment. `status` reuses
    `alpha_lab.evidence_coverage.CoverageStatus`'s vocabulary (FULL/
    PARTIAL/NO_EVIDENCE/NOT_COMPUTED/NOT_APPLICABLE) for the per-security
    domains it aggregates, plus two values specific to a domain this
    module reads directly rather than via that per-security model: OK/
    STALE for `core` (has an established freshness policy), and FULL/
    NOT_COMPUTED for the two global (not per-security) domains, Donatien
    and Alignment, that `alpha_lab.evidence_coverage` deliberately excludes
    (see that module's own docstring for why)."""

    domain: str
    label: str
    scope: str  # "per_security" | "global"
    status: str
    policy_defined: bool
    coverage: float | None = None
    freshness: str | None = None
    evidence_count: int | None = None
    detail: str | None = None


class CoreRefreshSummary(BaseModel):
    # True when core was never touched this run because nothing was stale
    # and neither an explicit `tickers` subset nor `force_core` was given
    # -- distinct from a real refresh that happened to attempt/succeed on
    # zero tickers (an empty tracked universe), and never a failure.
    skipped: bool
    tickers_attempted: int
    tickers_succeeded: int
    tickers_failed: int
    research_rebuilt: bool
    research_record_count: int = 0
    research_error: str | None = None
    # Why the ingestion batch ended early, and how many tickers it never got
    # to (see refresh.MAX_CONSECUTIVE_PROVIDER_FAILURES), plus the distinct
    # per-ticker failure reasons with counts. Display-only: deliberately left
    # out of the version hash (`_version_id`) so existing identities hold.
    stopped_early: str | None = None
    tickers_not_attempted: int = 0
    failure_reasons: dict[str, int] = {}


class ResearchRefreshStatus(BaseModel):
    """The orchestrator's own output -- what `current_research_refresh_
    status`/`research_refresh_status_snapshots` persist. `version_id` is
    the stable identity a future AI consumer cites as the exact research
    state it computed against."""

    version_id: str
    scope: str
    evaluation_date: date
    computed_at: datetime
    tracked_universe_size: int
    core: CoreRefreshSummary
    domains: list[DomainFreshness]
    methodology_version: str = METHODOLOGY_VERSION


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _version_id(*, evaluation_date: date, core: CoreRefreshSummary, domains: list[DomainFreshness]) -> str:
    """Deterministic identity for one research state. Deliberately
    excludes `computed_at` -- an AlphaLab-side wall-clock timestamp, not
    evidence content -- so re-running with nothing actually changed is
    idempotent rather than minting a new version every call, mirroring
    `alpha_lab.calibration.service._snapshot_id`'s own exclusion of
    `retrieved_at`/`created_at` for the identical reason."""
    identity = {
        "evaluation_date": evaluation_date.isoformat(),
        "core": core.model_dump(exclude={"stopped_early", "tickers_not_attempted", "failure_reasons"}),
        "domains": [
            domain.model_dump(exclude={"detail"})
            for domain in sorted(domains, key=lambda item: item.domain)
        ],
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _status_for_counts(*, total: int, full: int, no_evidence: int, not_computed: int, not_applicable: int) -> str:
    if total == 0 or not_computed + not_applicable == total:
        return "NOT_COMPUTED" if not_computed >= not_applicable else "NOT_APPLICABLE"
    if full + not_applicable == total:
        return "FULL"
    if no_evidence + not_computed + not_applicable == total and no_evidence > 0:
        return "NO_EVIDENCE"
    return "PARTIAL"


def _aggregate_per_security_domains(flat_rows: list[dict]) -> list[DomainFreshness]:
    """One `DomainFreshness` per `alpha_lab.evidence_coverage.CoverageRow`
    category, aggregated across every tracked security this orchestrator
    just read -- freshest observation, mean coverage, and a rolled-up
    status, reusing exactly the rows the Evidence & Coverage Dashboard
    page already computes (PR #28) rather than a second coverage engine."""
    if not flat_rows:
        return []
    frame = pd.DataFrame(flat_rows).drop_duplicates(subset=["ticker", "category"])
    domains: list[DomainFreshness] = []
    for category, group in frame.groupby("category"):
        total = len(group)
        full = int((group["status"] == "FULL").sum())
        no_evidence = int((group["status"] == "NO_EVIDENCE").sum())
        not_computed = int((group["status"] == "NOT_COMPUTED").sum())
        not_applicable = int((group["status"] == "NOT_APPLICABLE").sum())
        coverage_values = group.loc[group["status"] != "NOT_APPLICABLE", "coverage"].dropna()
        freshness_values = group["freshness"].dropna()
        domains.append(
            DomainFreshness(
                domain=category,
                label=str(group["label"].iloc[0]),
                scope="per_security",
                status=_status_for_counts(
                    total=total, full=full, no_evidence=no_evidence,
                    not_computed=not_computed, not_applicable=not_applicable,
                ),
                policy_defined=False,
                coverage=float(coverage_values.mean()) if not coverage_values.empty else None,
                freshness=_iso(max(freshness_values)) if not freshness_values.empty else None,
                evidence_count=int(group["evidence_count"].dropna().sum()) if group["evidence_count"].notna().any() else None,
                detail=f"{full}/{total} full coverage, {not_computed} not computed, {no_evidence} no evidence",
            )
        )
    return sorted(domains, key=lambda item: item.domain)


class ResearchRefreshOrchestrator:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine = engine
        self.settings = settings

    def run(
        self, *, tickers: list[str] | None = None, force_core: bool = False, scope: str = DEFAULT_SCOPE,
    ) -> ResearchRefreshStatus:
        """Core refresh-if-stale (unchanged `run_core_refresh`/`is_universe_
        price_stale`, same semantics and same network-call cost profile as
        before this orchestrator existed -- see `_maybe_refresh_core`) ->
        read-only cross-domain evidence status across the FULL tracked
        universe (independent of whatever subset `tickers` restricted core
        ingestion to -- a research state describes what AlphaLab currently
        knows, not just what this one call happened to touch) -> stamp +
        persist the resulting version.

        `tickers`/`force_core` reproduce the two existing call shapes
        exactly: the dashboard's own auto-trigger already computes its own
        stale subset and passes it as `tickers` (always attempted, same as
        `run_core_refresh(tickers=...)` today); the manual Full Refresh
        button forces a refresh regardless of staleness via `force_core=
        True` (same as its own unconditional `run_core_refresh_guarded`
        call today); `launch.py`'s launch-time check passes neither, so
        core is only touched -- and only then does a provider get called
        at all -- when `is_universe_price_stale` says so, exactly as
        before.
        """
        evaluation_date = date.today()
        core_result = self._maybe_refresh_core(tickers=tickers, force=force_core)
        core = (
            CoreRefreshSummary(
                skipped=False,
                tickers_attempted=len(core_result.tickers_attempted),
                tickers_succeeded=len(core_result.tickers_succeeded),
                tickers_failed=len(core_result.tickers_failed),
                research_rebuilt=core_result.research_rebuilt,
                research_record_count=core_result.research_record_count,
                research_error=core_result.research_error,
                stopped_early=core_result.stopped_early,
                tickers_not_attempted=len(core_result.tickers_not_attempted),
                failure_reasons=dict(Counter(core_result.tickers_failed.values())),
            )
            if core_result is not None
            else CoreRefreshSummary(skipped=True, tickers_attempted=0, tickers_succeeded=0, tickers_failed=0, research_rebuilt=False)
        )
        tracked_universe = refresh.configured_universe_tickers(self.engine)
        domains = self._read_domain_status(tracked_universe, evaluation_date)
        status = ResearchRefreshStatus(
            version_id="",
            scope=scope,
            evaluation_date=evaluation_date,
            computed_at=datetime.now(UTC),
            tracked_universe_size=len(tracked_universe),
            core=core,
            domains=domains,
        )
        status.version_id = _version_id(evaluation_date=evaluation_date, core=core, domains=domains)
        self._persist(status)
        return status

    def _maybe_refresh_core(self, *, tickers: list[str] | None, force: bool) -> CoreRefreshResult | None:
        """`None` means core was skipped entirely -- no provider was ever
        called, exactly reproducing `launch.py`'s existing behavior of
        never touching the network when nothing is stale.

        Calls `alpha_lab.refresh` via the module reference (`refresh.
        run_core_refresh(...)`, not a `from alpha_lab.refresh import
        run_core_refresh` name captured once at import time): existing
        tests monkeypatch `alpha_lab.refresh.run_core_refresh` itself by
        string path (e.g. to assert it's never called on a normal
        rerun), which only ever patches the attribute on that module
        object -- a statically-imported name elsewhere keeps pointing at
        whatever function object it captured on this module's own first
        import, silently surviving (or, worse, permanently capturing) an
        unrelated test's patch for the rest of the process. Going through
        the module reference makes every call here see exactly what
        `alpha_lab.refresh.run_core_refresh` currently is, real or
        patched, with no such leak either way."""
        if tickers is not None:
            return refresh.run_core_refresh(self.engine, self.settings, tickers=tickers)
        if force:
            return refresh.run_core_refresh(self.engine, self.settings)
        stale_after_days = self.settings.data_quality["stale_price_days"]
        if refresh.is_universe_price_stale(self.engine, stale_after_days):
            return refresh.run_core_refresh(self.engine, self.settings)
        return None

    def _read_domain_status(self, tracked_universe: list[str], evaluation_date: date) -> list[DomainFreshness]:
        research_service = ResearchService(self.engine, self.settings)
        news_service = NewsService(self.engine)
        macro_service = MacroRegimeService(self.engine)
        calibration_service = ExternalCalibrationService(self.engine)
        alignment_service = AlignmentService(self.engine)

        records_by_ticker = {
            record.ticker: record
            for record in research_service.list_current_research()
            if record.ticker in tracked_universe
        }
        news_by_ticker = news_service.get_history_for_tickers(tracked_universe)
        macro_assessment = macro_service.get_current_assessment()

        summaries = []
        for ticker in tracked_universe:
            record = records_by_ticker.get(ticker)
            if record is None:
                continue
            research = research_service.build_research_for_record(record)
            summaries.append(
                build_security_coverage_summary(
                    research,
                    news_articles=news_by_ticker.get(ticker, []),
                    macro_assessment=macro_assessment,
                )
            )
        domains = _aggregate_per_security_domains(flatten_coverage_rows(summaries))

        donatien = calibration_service.get_current()
        domains.append(
            DomainFreshness(
                domain="donatien_calibration",
                label="Donatien External Calibration",
                scope="global",
                status="FULL" if donatien is not None else "NOT_COMPUTED",
                policy_defined=False,
                freshness=_iso(donatien.retrieved_at) if donatien is not None else None,
                detail=None if donatien is not None else "never refreshed (scripts/refresh_donatien_calibration.py)",
            )
        )
        alignment = alignment_service.get_current()
        domains.append(
            DomainFreshness(
                domain="alignment",
                label="Donatien <-> Macro Regime Alignment",
                scope="global",
                status="FULL" if alignment is not None else "NOT_COMPUTED",
                policy_defined=False,
                freshness=_iso(alignment.computed_at) if alignment is not None else None,
                detail=alignment.alignment if alignment is not None else "never computed (scripts/refresh_alignment.py)",
            )
        )
        return domains

    def _persist(self, status: ResearchRefreshStatus) -> None:
        payload = status.model_dump(mode="json")
        with Session(self.engine) as session:
            existing = session.get(CurrentResearchRefreshStatus, status.scope)
            content_changed = existing is None or existing.version_id != status.version_id

            if content_changed:
                snapshot_row = session.scalar(
                    select(ResearchRefreshStatusSnapshot).where(
                        ResearchRefreshStatusSnapshot.version_id == status.version_id
                    )
                )
                if snapshot_row is None:
                    session.add(
                        ResearchRefreshStatusSnapshot(
                            version_id=status.version_id,
                            scope=status.scope,
                            evaluation_date=status.evaluation_date,
                            payload=payload,
                        )
                    )
                    try:
                        session.flush()
                    except IntegrityError:
                        # A concurrent orchestrator run persisted the identical
                        # version first; the unique constraint on version_id is
                        # the actual idempotency guarantee. Re-fetch `existing`
                        # -- rollback() expires every object the session was
                        # tracking, so the reference from before this block
                        # must never be reused as-is (mirrors alpha_lab.
                        # calibration.service.ExternalCalibrationService.
                        # refresh's own identical recovery).
                        session.rollback()
                        existing = session.get(CurrentResearchRefreshStatus, status.scope)

            if existing is None:
                existing = CurrentResearchRefreshStatus(scope=status.scope)
                session.add(existing)
            existing.version_id = status.version_id
            existing.evaluation_date = status.evaluation_date
            existing.payload = payload
            existing.computed_at = status.computed_at
            session.commit()


def get_current_research_refresh_status(engine: Engine, *, scope: str = DEFAULT_SCOPE) -> ResearchRefreshStatus | None:
    """Pure DB read, no network, no computation -- the current research
    state a future AI consumer (roadmap Phase 6) would cite by
    `version_id`, or `None` if the orchestrator has never run."""
    with Session(engine) as session:
        row = session.get(CurrentResearchRefreshStatus, scope)
        if row is None:
            return None
        return ResearchRefreshStatus.model_validate(row.payload)


def run_research_refresh_guarded(
    engine: Engine, settings: Settings, state: dict[str, Any], *, tickers: list[str] | None = None,
    force_core: bool = False, scope: str = DEFAULT_SCOPE,
) -> ResearchRefreshStatus | None:
    """Same operation as `ResearchRefreshOrchestrator.run`, refusing to
    start a second one while `state` already records one in progress --
    identical guard shape to `alpha_lab.refresh.run_core_refresh_guarded`
    (same session-scoped, not cross-process, limitation; see that
    function's own docstring). Returns `None` without calling any
    provider when a refresh is already in progress."""
    if state.get("research_refresh_in_progress"):
        return None
    state["research_refresh_in_progress"] = True
    try:
        return ResearchRefreshOrchestrator(engine, settings).run(
            tickers=tickers, force_core=force_core, scope=scope
        )
    finally:
        state["research_refresh_in_progress"] = False
