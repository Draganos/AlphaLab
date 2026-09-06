"""Read-only boundary exposing the canonical StockResearch research contract.

This is the only supported way for UI or other consumers to obtain a
StockResearch object. It performs no scoring, no provider calls, and no
persistence: it reads the already-persisted current research snapshot via
``alpha_lab.screener.service.MarketScreenerService.read_current_research``
(itself a pure database read — see that method's docstring) and converts
only the requested ticker's record through
``alpha_lab.research.build.build_stock_research``. The whole universe is
never converted merely to render one security.
"""

from sqlalchemy import Engine

from alpha_lab.config import Settings
from alpha_lab.research.build import build_stock_research
from alpha_lab.research.model import StockResearch
from alpha_lab.screener.service import LiveResearchRecord, MarketScreenerService


class ResearchService:
    def __init__(self, engine: Engine, settings: Settings):
        self._screener = MarketScreenerService(engine, settings)

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
        """Return the canonical StockResearch for one ticker.

        Converts only the matching persisted record — never the whole
        universe. Returns ``None`` when no current research snapshot exists
        for the ticker at all, which is distinct from a StockResearch whose
        categories/metrics are UNAVAILABLE (research exists; evidence is
        simply missing).
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
