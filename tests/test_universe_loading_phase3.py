from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Security
from alpha_lab.ingestion import UniverseIngestionService
from alpha_lab.providers.interfaces import SecurityUniverseProvider


class FixtureUniverse(SecurityUniverseProvider):
    def get_securities(self, *, country="US", exchanges=()):
        return [
            {
                "ticker": f"Q{i:03}",
                "company_name": f"Nasdaq {i}",
                "exchange": "NASDAQ",
                "country": "US",
                "asset_type": "equity",
                "metadata_provider": "fixture",
                "metadata_source": "offline broad-universe fixture",
            }
            for i in range(350)
        ] + [
            {
                "ticker": f"N{i:03}",
                "company_name": f"Nyse {i}",
                "exchange": "NYSE",
                "country": "US",
                "asset_type": "equity",
                "metadata_provider": "fixture",
                "metadata_source": "offline broad-universe fixture",
            }
            for i in range(350)
        ]

    def refresh_metadata(self, tickers):
        return []


def test_full_universe_is_stored_and_limited_subset_is_exchange_balanced(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'universe.db'}")
    try:
        create_schema(engine)
        service = UniverseIngestionService(FixtureUniverse(), engine)
        assert service.load(limit=300) == 700
        subset = service.research_tickers(limit=300)
        with Session(engine) as session:
            assert session.scalar(select(func.count(Security.ticker))) == 700
            exchanges = {session.get(Security, ticker).exchange for ticker in subset}
            assert exchanges == {"NASDAQ", "NYSE"}
            assert subset[:4] == ["Q000", "N000", "Q001", "N001"]
    finally:
        engine.dispose()


def test_enrichment_preserves_canonical_exchange_and_research_membership(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'canonical.db'}")
    try:
        create_schema(engine)
        service = UniverseIngestionService(FixtureUniverse(), engine)
        service.load()
        assert service.enrich([{"ticker": "Q000", "exchange": "NMS", "sector": "Tech"}]) == 1
        assert service.enrich([{"ticker": "N000", "exchange": "NYQ", "sector": "Finance"}]) == 1
        with Session(engine) as session:
            assert session.get(Security, "Q000").exchange == "NASDAQ"
            assert session.get(Security, "N000").exchange == "NYSE"
        assert {"Q000", "N000"} <= set(service.research_tickers(limit=None))
    finally:
        engine.dispose()
