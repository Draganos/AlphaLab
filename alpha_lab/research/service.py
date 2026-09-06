"""Read-only boundary exposing the canonical StockResearch research contract.

This is the only supported way for UI or other consumers to obtain a
StockResearch object. It performs no scoring, no provider calls, and (for
the *current* research path) no persistence: it reads the already-persisted
current research snapshot via
``alpha_lab.screener.service.MarketScreenerService.read_current_research``
(itself a pure database read — see that method's docstring) and converts
only the requested ticker's record through
``alpha_lab.research.build.build_stock_research``. The whole universe is
never converted merely to render one security.

This service also owns the boundary between *current* and *historical*
research (see module docstring in ``alpha_lab.research.snapshots``):

- ``get_stock_research`` — current: whatever the live screener would show
  now, recomputed from the latest persisted screener build each call.
- ``persist_snapshot`` — the only path that writes a historical snapshot;
  callers decide when history is recorded (see its docstring). Never
  called implicitly by a read.
- ``get_research_snapshot`` / ``get_latest_snapshot`` / ``get_research_history``
  — historical: immutable, previously persisted StockResearch objects,
  never rebuilt from current data.
- ``compare_snapshots`` — loads two historical snapshots and diffs them.

Callers never need to know a database or repository is involved.
"""

from sqlalchemy import Engine

from alpha_lab.config import Settings
from alpha_lab.research.build import build_stock_research
from alpha_lab.research.comparison import ResearchComparison, compare_stock_research
from alpha_lab.research.model import ResearchSnapshotSummary, StockResearch
from alpha_lab.research.snapshots import ResearchSnapshotRepository
from alpha_lab.screener.service import LiveResearchRecord, MarketScreenerService


class ResearchService:
    def __init__(self, engine: Engine, settings: Settings):
        self._screener = MarketScreenerService(engine, settings)
        self._snapshots = ResearchSnapshotRepository(engine)

    # --- Current research (live, not persisted history) -------------------

    def list_current_research(self) -> list[LiveResearchRecord]:
        """Thin passthrough to the existing persisted-snapshot read path.

        Returns the raw records only for identity/selection purposes (e.g.
        populating a ticker picker) and for the handful of quote-level
        fields (price, market cap, ethical status, last refresh) that are
        deliberately outside the canonical StockResearch evidence contract.
        Ordinary rendering of research content should use
        ``get_stock_research`` instead.
        """
        return self._screener.read_current_research()

    def get_stock_research(self, ticker: str) -> StockResearch | None:
        """Return the canonical, *current* StockResearch for one ticker.

        Converts only the matching persisted record — never the whole
        universe. Returns ``None`` when no current research snapshot exists
        for the ticker at all, which is distinct from a StockResearch whose
        categories/metrics are UNAVAILABLE (research exists; evidence is
        simply missing). This never touches historical snapshot storage.
        """
        record = self._find_record(ticker)
        return None if record is None else build_stock_research(record)

    def _find_record(self, ticker: str) -> LiveResearchRecord | None:
        normalized = ticker.strip().upper()
        return next(
            (
                record
                for record in self.list_current_research()
                if record.ticker == normalized
            ),
            None,
        )

    # --- Historical snapshots -----------------------------------------

    def persist_snapshot(self, stock_research: StockResearch) -> ResearchSnapshotSummary:
        """Explicitly persist one immutable historical snapshot.

        This is the only write path for research history. It must be
        called deliberately (e.g. a "Save research snapshot" action) —
        never from an ordinary read/render path, and never implicitly from
        ``get_stock_research``. Performs no provider calls and no scoring;
        it only serializes and stores the ``StockResearch`` it is given.
        Idempotent: persisting identical research content again returns the
        existing snapshot rather than creating a duplicate. Raises on a
        genuine storage failure — callers must not treat that as success.
        """
        return self._snapshots.save(stock_research)

    def get_research_snapshot(self, snapshot_id: str) -> StockResearch | None:
        """The exact historical StockResearch for one snapshot ID, or None.

        Never rebuilt from current data — this is the frozen payload as it
        was persisted.
        """
        return self._snapshots.get(snapshot_id)

    def get_latest_snapshot(self, ticker: str) -> StockResearch | None:
        """The most recently *persisted* historical snapshot for a ticker.

        Distinct from ``get_stock_research``: this can lag behind current
        research if no snapshot has been persisted since the last refresh.
        """
        return self._snapshots.get_latest(ticker)

    def get_research_history(self, ticker: str) -> list[ResearchSnapshotSummary]:
        """Lightweight snapshot history for a ticker, newest first.

        Queries by ticker at the database level and never deserializes
        full StockResearch payloads for entries the caller hasn't selected
        — use ``get_research_snapshot`` for one entry's full detail.
        """
        return self._snapshots.list_for_ticker(ticker)

    def compare_snapshots(self, older_id: str, newer_id: str) -> ResearchComparison | None:
        """Diff two persisted snapshots. Returns None if either is missing."""
        older = self._snapshots.get(older_id)
        newer = self._snapshots.get(newer_id)
        if older is None or newer is None:
            return None
        return compare_stock_research(older, newer)
