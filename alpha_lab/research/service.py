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

from datetime import date

from sqlalchemy import Engine

from alpha_lab.config import Settings
from alpha_lab.research.analyst_events import AnalystEventsService
from alpha_lab.research.build import build_stock_research
from alpha_lab.research.comparison import ResearchComparison, compare_stock_research
from alpha_lab.research.model import ResearchSnapshotSummary, StockResearch
from alpha_lab.research.snapshots import ResearchSnapshotRepository
from alpha_lab.research.supplemental_service import SupplementalResearchService
from alpha_lab.screener.service import LiveResearchRecord, MarketScreenerService


class ResearchService:
    def __init__(self, engine: Engine, settings: Settings):
        self._screener = MarketScreenerService(engine, settings)
        self._snapshots = ResearchSnapshotRepository(engine)
        self._supplemental = SupplementalResearchService(engine)
        self._analyst_events = AnalystEventsService(engine)

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

        Note for a caller iterating many tickers (e.g. a universe-wide
        view): finding the one matching record still reads and
        re-deserializes every persisted record via ``list_current_research``
        (see ``_find_record``), so calling this once per ticker in a loop is
        O(n²) in universe size. Call ``list_current_research()`` once and
        pass each record to ``build_research_for_record`` instead — see
        that method's docstring.
        """
        record = self._find_record(ticker)
        if record is None:
            return None
        return self.build_research_for_record(record)

    def build_research_for_record(self, record: LiveResearchRecord) -> StockResearch:
        """The exact enrichment `get_stock_research` performs, for a caller
        that already has the matching `LiveResearchRecord` (typically from
        one `list_current_research()` call) and wants to build
        `StockResearch` for many tickers without re-reading and
        re-deserializing the whole persisted universe once per ticker.

        Enriched with the current Analyst Consensus / Technical Summary /
        AI Research Rating / Analyst Research (rating changes + revision
        trend) / Fund Evidence, if any have been computed for this ticker —
        a pure database read of each (see ``SupplementalResearchService``/
        ``AnalystEventsService``), never a provider call and never a
        recomputation. Any of the five can be ``None`` independently; that
        never affects the fundamental score/categories/coverage above,
        which are computed and read entirely separately.
        """
        ticker = record.ticker
        research = build_stock_research(record)
        return research.model_copy(
            update={
                "analyst_consensus": self._supplemental.get_analyst_consensus(ticker),
                "technical_summary": self._supplemental.get_technical_summary(ticker),
                "ai_research_assessment": self._supplemental.get_ai_research_assessment(ticker),
                "analyst_research": self._analyst_events.get_research_summary(ticker),
                "fund_evidence": self._supplemental.get_fund_evidence(ticker),
            }
        )

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

    def snapshot_current_research(self, ticker: str) -> ResearchSnapshotSummary | None:
        """Re-read current research and persist an automatic historical
        snapshot of it -- or do nothing if no current research exists for
        this ticker at all. Idempotent (``persist_snapshot`` dedupes
        identical content), so calling this after a refresh that changed
        nothing never creates a duplicate.

        This is how Analyst Consensus/Technical Summary/AI Research
        Rating/Fund Evidence -- none of which has a history table of its
        own -- start accumulating real history from *ordinary* refreshes,
        rather than requiring a separate manual "Save research snapshot"
        click every time. Both of this codebase's supplemental-refresh
        entry points call this right after their own refresh completes:
        the Company Research page's "Refresh for this ticker" button, and
        ``scripts/refresh_supplemental_research.py``'s batch run -- see
        ARCHITECTURE.md's "Historical Research Reconstruction" section for
        why a single shared call site matters here (an earlier version of
        this wired only the UI button, silently leaving the batch script's
        routine use out of history entirely).
        """
        research = self.get_stock_research(ticker)
        if research is None:
            return None
        return self.persist_snapshot(research)

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

    def get_latest_snapshot_as_of(self, ticker: str, as_of: date) -> StockResearch | None:
        """Point-in-time historical lookup: the most recent snapshot that
        genuinely existed by ``as_of`` (see
        ``ResearchSnapshotRepository.get_latest_as_of``'s own docstring for
        the PIT rationale). ``None`` means no snapshot had been saved for
        this ticker by that date -- an honest capability gap, not an error,
        for any date before automatic/manual snapshotting started."""
        return self._snapshots.get_latest_as_of(ticker, as_of)

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
