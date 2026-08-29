from datetime import date
from sqlalchemy import inspect, select, text
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


def test_ethics_fingerprint_migration_is_additive_and_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'phase3-current.db'}")
    try:
        create_schema(engine)
        with engine.begin() as connection:
            connection.execute(
                text("DROP INDEX IF EXISTS ix_ethical_evaluations_evidence_fingerprint")
            )
            connection.execute(
                text("ALTER TABLE ethical_evaluations DROP COLUMN evidence_fingerprint")
            )
            connection.execute(
                text("INSERT INTO securities (ticker) VALUES ('LEGACY')")
            )
            connection.execute(
                text(
                    "INSERT INTO ethical_evaluations "
                    "(ticker, ethical_status, business_tags, exclusion_reasons, review_reasons, evidence, "
                    "evaluated_at, policy_version, manual_override, financial_warnings) VALUES "
                    "('LEGACY', 'REVIEW', '[]', '[]', '[]', '[]', CURRENT_TIMESTAMP, 'old-policy', 0, '[]')"
                )
            )
        create_schema(engine)
        create_schema(engine)
        assert "evidence_fingerprint" in {
            column["name"]
            for column in inspect(engine).get_columns("ethical_evaluations")
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM ethical_evaluations WHERE ticker='LEGACY'"
                    )
                )
                == 1
            )
    finally:
        engine.dispose()
