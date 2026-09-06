"""Durable, append-only, immutable persistence for canonical StockResearch.

This is the historical counterpart to the live/current path
(``alpha_lab.screener.service.MarketScreenerService`` +
``CurrentResearchBuild``/``CurrentResearchSnapshot``, which persist the
pre-canonical ``LiveResearchRecord`` and are read via
``ResearchService.list_current_research``/``get_stock_research``). That
existing mechanism already is append-only (every rebuild inserts a new
``CurrentResearchBuild`` header and new snapshot rows; nothing is ever
updated in place) — but it is scoped to the live screener and stores a
different payload shape, and only the *latest* build is ever read back. It
is deliberately not reused here: this table stores the fully-materialized
``StockResearch`` (categories, evidence, confidence — everything PR #13/#14
computed), not the raw record that produces it, so a historical snapshot
can never be affected by a future change to ``build_stock_research``'s
logic (formula fixes, weight changes, provider-capability updates, ...).
A snapshot generated under one schema version stays interpretable as that
version even after the code evolves — see ``RESEARCH_SCHEMA_VERSION``.

``ResearchSnapshotRepository`` only ever appends. There is deliberately no
update/delete method: immutability is architectural, not a convention to
remember.
"""

import hashlib
import json
from datetime import date

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.database.models import ResearchSnapshot
from alpha_lab.research.model import ResearchSnapshotSummary, StockResearch

# Bump when StockResearch's shape changes in a way that could change how a
# stored payload should be interpreted. Existing rows keep their original
# value forever; this module never rewrites a persisted schema version.
RESEARCH_SCHEMA_VERSION = "stockresearch-v1"


class ResearchSnapshotRepository:
    """Append-only repository for historical StockResearch snapshots."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def save(self, stock_research: StockResearch) -> ResearchSnapshotSummary:
        """Persist one immutable snapshot; idempotent on identical content.

        Never calls providers, never recomputes scores, never mutates
        ``stock_research`` — it only serializes and stores what it is
        given. Raises on a genuine storage failure rather than swallowing
        it; callers must not treat a failed save as if it succeeded.
        """
        payload = stock_research.model_dump(mode="json")
        payload_hash = _payload_hash(payload)
        snapshot_id = _snapshot_id(
            ticker=stock_research.ticker,
            evaluation_date=stock_research.evaluation_date,
            rating_version=stock_research.rating_version,
            configuration_hash=stock_research.configuration_hash,
            payload_hash=payload_hash,
        )
        existing = self._get_row(snapshot_id)
        if existing is not None:
            return _summary(existing)
        row = ResearchSnapshot(
            snapshot_id=snapshot_id,
            ticker=stock_research.ticker,
            evaluation_date=stock_research.evaluation_date,
            generated_at=stock_research.generated_at,
            rating_version=stock_research.rating_version,
            configuration_hash=stock_research.configuration_hash,
            research_schema_version=RESEARCH_SCHEMA_VERSION,
            overall_score=stock_research.overall_score,
            overall_coverage=stock_research.overall_coverage,
            confidence=stock_research.confidence,
            confidence_label=stock_research.confidence_label,
            data_quality_status=stock_research.data_quality_status,
            payload_hash=payload_hash,
            payload=payload,
        )
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent caller persisted the identical content first;
                # the unique constraint on snapshot_id is the actual
                # idempotency guarantee, this is not an application-level
                # existence check racing against it.
                session.rollback()
                existing = self._get_row(snapshot_id)
                if existing is None:
                    raise
                return _summary(existing)
            return _summary(row)

    def get(self, snapshot_id: str) -> StockResearch | None:
        """Return the exact StockResearch as persisted, or None if unknown.

        Never rebuilt from current data — this deserializes the frozen
        payload as-is.
        """
        row = self._get_row(snapshot_id)
        return None if row is None else StockResearch.model_validate(row.payload)

    def get_latest(self, ticker: str) -> StockResearch | None:
        """The most recently persisted snapshot for a ticker, or None."""
        normalized = ticker.strip().upper()
        with Session(self.engine) as session:
            row = session.scalar(
                select(ResearchSnapshot)
                .where(ResearchSnapshot.ticker == normalized)
                .order_by(
                    ResearchSnapshot.evaluation_date.desc(),
                    ResearchSnapshot.created_at.desc(),
                    ResearchSnapshot.id.desc(),
                )
                .limit(1)
            )
            return None if row is None else StockResearch.model_validate(row.payload)

    def list_for_ticker(
        self,
        ticker: str,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[ResearchSnapshotSummary]:
        """Lightweight history metadata, newest first — no full payloads.

        Queries by ticker (and optionally an evaluation_date range) at the
        database level; never loads other tickers' snapshots to filter in
        Python.
        """
        normalized = ticker.strip().upper()
        statement = select(ResearchSnapshot).where(ResearchSnapshot.ticker == normalized)
        if start is not None:
            statement = statement.where(ResearchSnapshot.evaluation_date >= start)
        if end is not None:
            statement = statement.where(ResearchSnapshot.evaluation_date <= end)
        statement = statement.order_by(
            ResearchSnapshot.evaluation_date.desc(),
            ResearchSnapshot.created_at.desc(),
            ResearchSnapshot.id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        with Session(self.engine) as session:
            return [_summary(row) for row in session.scalars(statement)]

    def exists(self, snapshot_id: str) -> bool:
        return self._get_row(snapshot_id) is not None

    def _get_row(self, snapshot_id: str) -> ResearchSnapshot | None:
        with Session(self.engine) as session:
            return session.scalar(
                select(ResearchSnapshot).where(ResearchSnapshot.snapshot_id == snapshot_id)
            )


def _summary(row: ResearchSnapshot) -> ResearchSnapshotSummary:
    return ResearchSnapshotSummary(
        snapshot_id=row.snapshot_id,
        ticker=row.ticker,
        evaluation_date=row.evaluation_date,
        generated_at=row.generated_at,
        rating_version=row.rating_version,
        configuration_hash=row.configuration_hash,
        research_schema_version=row.research_schema_version,
        overall_score=row.overall_score,
        overall_coverage=row.overall_coverage,
        confidence=row.confidence,
        confidence_label=row.confidence_label,
        data_quality_status=row.data_quality_status,
        payload_hash=row.payload_hash,
        created_at=row.created_at,
    )


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(payload: dict) -> str:
    """Hash the payload with `generated_at` excluded — that field is the
    computation timestamp, not part of the research content, and must not
    make two otherwise-identical research states hash differently."""
    content = {key: value for key, value in payload.items() if key != "generated_at"}
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _snapshot_id(
    *,
    ticker: str,
    evaluation_date: date,
    rating_version: str,
    configuration_hash: str,
    payload_hash: str,
) -> str:
    identity = {
        "ticker": ticker,
        "evaluation_date": evaluation_date.isoformat(),
        "rating_version": rating_version,
        "configuration_hash": configuration_hash,
        "payload_hash": payload_hash,
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()
