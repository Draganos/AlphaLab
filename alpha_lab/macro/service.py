"""Persistence/orchestration for AlphaLab Macro Regime.

Deliberately separate from `alpha_lab.research`/`alpha_lab.screener`/
`alpha_lab.strategy`/`alpha_lab.backtest`/`alpha_lab.portfolio`: nothing in
any of those modules imports from here, and this module never touches
`StockResearch`, `LiveResearchRecord`, `composite_score`, or ranking. See
`alpha_lab.macro.regime`'s module docstring for the full scope rationale.

Unlike Donatien (a single external fetch), macro regime is computed from
AlphaLab's own already-ingested Price history for a small fixed set of
market-proxy tickers -- so `refresh()` first ingests fresh price data for
those tickers via the existing `IngestionService` (reusing it exactly as
any other ticker would be ingested), then computes the assessment purely
from stored data, with zero network calls at computation time.

Read methods here are pure database reads -- safe to call from a Streamlit
render. `refresh` calls a provider (via IngestionService) and is meant to be
triggered explicitly (a script or a UI button), never from an ordinary page
render.
"""

from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.database.models import CurrentMacroAssessment, MacroAssessmentSnapshot, Price
from alpha_lab.ingestion.service import IngestionService
from alpha_lab.macro.regime import (
    MACRO_METHODOLOGY_VERSION,
    MACRO_PROXY_TICKERS,
    MacroAssessment,
    build_macro_assessment,
)
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError

DEFAULT_MACRO_SCOPE = "US"


def _canonical_json(data: object) -> str:
    import json

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _content_hash(assessment: MacroAssessment) -> str:
    """Hash only the substantive regime content -- deliberately excludes
    every `as_of` (top-level and per-indicator, since the "latest price
    date" advances daily even when a proxy's value is genuinely flat) for
    the same reason alpha_lab.research.snapshots._payload_hash excludes
    `generated_at`: a timestamp advancing by itself is not a change in what
    was actually observed, and must not defeat hash-based deduplication."""
    substantive = {
        "scope": assessment.scope,
        "regime": assessment.regime.value,
        "regime_score": assessment.regime_score,
        "confidence": assessment.confidence,
        "coverage": assessment.coverage,
        "methodology_version": assessment.methodology_version,
        "indicators": [
            {
                "name": indicator.name,
                "category": indicator.category.value,
                "ticker": indicator.ticker,
                "value": indicator.value,
                "signal": indicator.signal,
                "source": indicator.source,
            }
            for indicator in assessment.indicators
        ],
    }
    import hashlib

    return hashlib.sha256(_canonical_json(substantive).encode()).hexdigest()


def _snapshot_id(*, scope: str, content_hash: str) -> str:
    import hashlib

    identity = {"scope": scope, "content_hash": content_hash}
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


class MacroRegimeService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation -------------------------

    def get_current(self, scope: str = DEFAULT_MACRO_SCOPE) -> CurrentMacroAssessment | None:
        with Session(self.engine) as session:
            row = session.get(CurrentMacroAssessment, scope)
            if row is not None:
                session.expunge(row)
            return row

    def get_current_assessment(self, scope: str = DEFAULT_MACRO_SCOPE) -> MacroAssessment | None:
        row = self.get_current(scope)
        return None if row is None else MacroAssessment.model_validate(row.payload)

    def get_history(
        self, scope: str = DEFAULT_MACRO_SCOPE, *, limit: int | None = None
    ) -> list[MacroAssessmentSnapshot]:
        with Session(self.engine) as session:
            statement = (
                select(MacroAssessmentSnapshot)
                .where(MacroAssessmentSnapshot.scope == scope)
                .order_by(MacroAssessmentSnapshot.created_at.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            session.expunge_all()
            return list(rows)

    # --- refresh: explicit, ingestion happens before any assessment write ---

    def refresh(
        self,
        provider: MarketDataProvider,
        *,
        scope: str = DEFAULT_MACRO_SCOPE,
        lookback_days: int = 400,
        as_of: date | None = None,
    ) -> CurrentMacroAssessment:
        """Ingest fresh price history for the fixed macro-proxy tickers
        (via the existing IngestionService -- no new ingestion path), then
        compute and persist the assessment purely from stored data.

        A ticker that fails to ingest (ProviderError) is skipped for this
        refresh -- it is reported as UNAVAILABLE in the resulting
        assessment (reduced coverage), never as a reason to abort the whole
        refresh or fabricate a value. This mirrors the existing "missing
        != erroring" evidence-integrity rule.

        Point-in-time: only Price rows with `date <= as_of` are read when
        building each proxy's history, mirroring
        HistoricalScoringService's own PIT filtering. Without this, a
        database that already holds price rows dated after `as_of` (e.g.
        ingested by a later, unrelated refresh) could leak future
        observations into a supposedly historical assessment -- the same
        class of bug alpha_lab.database.queries.latest_fundamentals_as_of
        exists to prevent for fundamentals.
        """
        as_of = as_of or date.today()
        start = as_of.fromordinal(as_of.toordinal() - lookback_days)
        ingestion = IngestionService(provider, self.engine)
        for ticker in MACRO_PROXY_TICKERS:
            try:
                ingestion.ingest(ticker, start, as_of)
            except ProviderError:
                continue

        price_histories: dict[str, pd.DataFrame] = {}
        with Session(self.engine) as session:
            for ticker in MACRO_PROXY_TICKERS:
                rows = session.scalars(
                    select(Price)
                    .where(Price.ticker == ticker, Price.date <= as_of)
                    .order_by(Price.date)
                ).all()
                frame = pd.DataFrame(
                    [{"date": row.date, "close": row.close} for row in rows if row.close is not None]
                )
                if not frame.empty:
                    frame = frame.set_index("date")
                price_histories[ticker] = frame

        assessment = build_macro_assessment(scope=scope, price_histories=price_histories, as_of=as_of)
        payload = assessment.model_dump(mode="json")
        content_hash = _content_hash(assessment)

        with Session(self.engine) as session:
            existing = session.get(CurrentMacroAssessment, scope)
            content_changed = existing is None or existing.content_hash != content_hash

            if content_changed:
                snapshot_id = _snapshot_id(scope=scope, content_hash=content_hash)
                already_present = session.scalar(
                    select(MacroAssessmentSnapshot).where(
                        MacroAssessmentSnapshot.snapshot_id == snapshot_id
                    )
                )
                if already_present is None:
                    session.add(MacroAssessmentSnapshot(
                        snapshot_id=snapshot_id, scope=scope,
                        regime=assessment.regime.value, regime_score=assessment.regime_score,
                        confidence=assessment.confidence, coverage=assessment.coverage,
                        content_hash=content_hash, methodology_version=MACRO_METHODOLOGY_VERSION,
                        as_of=assessment.as_of, payload=payload,
                    ))
                    try:
                        session.flush()
                    except IntegrityError:
                        session.rollback()
                        existing = session.get(CurrentMacroAssessment, scope)

            if existing is None:
                existing = CurrentMacroAssessment(scope=scope)
                session.add(existing)
            existing.regime = assessment.regime.value
            existing.regime_score = assessment.regime_score
            existing.confidence = assessment.confidence
            existing.coverage = assessment.coverage
            existing.content_hash = content_hash
            existing.methodology_version = MACRO_METHODOLOGY_VERSION
            existing.as_of = assessment.as_of
            existing.payload = payload
            existing.computed_at = datetime.now(UTC)

            session.commit()
            session.refresh(existing)
            session.expunge(existing)
            return existing
