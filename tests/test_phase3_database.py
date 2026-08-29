from datetime import date
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Estimate, Security
from alpha_lab.ingestion.estimates import snapshot_estimates
from alpha_lab.phase3 import Phase3Repository


def test_phase3_schema_additive_idempotent_and_estimate_snapshots(tmp_path):
    db_engine = make_engine(f"sqlite:///{tmp_path / 'phase3.db'}")
    try:
        create_schema(db_engine)
        create_schema(db_engine)
        assert {
            "ethical_evaluations",
            "business_themes",
            "saved_screeners",
            "ai_research_analyses",
        } <= set(inspect(db_engine).get_table_names())
        with Session(db_engine) as session:
            session.add(Security(ticker="X"))
            session.commit()
        rows = [{"fiscal_period": date(2025, 12, 31), "consensus_eps": 2.0}]
        assert (
            snapshot_estimates(
                db_engine, "X", date(2025, 1, 1), rows, provider="fixture"
            )
            == 1
        )
        assert (
            snapshot_estimates(
                db_engine, "X", date(2025, 1, 1), rows, provider="fixture"
            )
            == 0
        )
        with Session(db_engine) as session:
            assert session.scalar(select(Estimate)).observation_hash
        repository = Phase3Repository(db_engine)
        assert (
            repository.save_screener("Quality", {"minimum_overall_score": 75}).name
            == "Quality"
        )
        repository.rename_screener("Quality", "Quality Growth")
        assert [item.name for item in repository.list_screeners()] == ["Quality Growth"]
        repository.delete_screener("Quality Growth")
        assert repository.list_screeners() == []
    finally:
        db_engine.dispose()
