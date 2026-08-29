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
                "ticker": f"T{i:03}",
                "company_name": f"Company {i}",
                "exchange": "NASDAQ",
                "country": "US",
                "asset_type": "equity",
                "metadata_provider": "fixture",
                "metadata_source": "offline broad-universe fixture",
            }
            for i in range(350)
        ]

    def refresh_metadata(self, tickers):
        return []


def test_broad_universe_loader_supports_hundreds_offline(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'universe.db'}")
    try:
        create_schema(engine)
        assert (
            UniverseIngestionService(FixtureUniverse(), engine).load(limit=300) == 300
        )
        with Session(engine) as session:
            assert session.scalar(select(func.count(Security.ticker))) == 300
            first = session.get(Security, "T000")
            assert first.metadata_provider == "fixture"
    finally:
        engine.dispose()
