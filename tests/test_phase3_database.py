from datetime import date, timedelta
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.ai import AIResearchResult
from alpha_lab.ai.research import EvidenceReference
from alpha_lab.ai.service import AIResearchService
from alpha_lab.database.models import (
    AIResearchAnalysis,
    BusinessTheme,
    CompanyDocument,
    Estimate,
    Security,
)
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
            "sec_company_facts",
            "current_research_builds",
            "current_research_snapshots",
        } <= set(inspect(db_engine).get_table_names())
        ai_columns = {
            column["name"]
            for column in inspect(db_engine).get_columns("ai_research_analyses")
        }
        assert {"analyzed_document_ids", "input_fingerprint"} <= ai_columns
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


def test_ai_cache_uses_complete_document_input_not_cited_subset(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ai-cache.db'}")

    class CountingProvider:
        def __init__(self):
            self.calls = 0

        def analyze(self, ticker, documents):
            self.calls += 1
            return AIResearchResult(
                guidance_score=0,
                demand_score=0,
                margin_outlook_score=0,
                competitive_position_score=0,
                management_confidence_score=0,
                balance_sheet_commentary_score=0,
                risk_score=0,
                sentiment_score=0,
                catalyst_score=0,
                evidence=[EvidenceReference(document_id=documents[0]["id"], excerpt="Source")],
                summary="Evidence analysis",
                provider="counting",
                model="fixture",
                prompt_version="v1",
                confidence=1,
            )

    try:
        create_schema(engine)
        with Session(engine) as session:
            session.add(Security(ticker="X"))
            session.add(
                CompanyDocument(
                    ticker="X",
                    document_date=date.today() - timedelta(days=1),
                    document_type="filing",
                    title="Document 1",
                    text="Text 1",
                )
            )
            session.commit()
        provider = CountingProvider()
        service = AIResearchService(engine, provider)
        assert service.ensure_all() == 1
        assert service.ensure_all() == 0
        assert provider.calls == 1
        with Session(engine) as session:
            analysis = session.scalar(select(AIResearchAnalysis))
            assert len(analysis.analyzed_document_ids) == 1
            assert len(analysis.source_document_ids) == 1
            assert analysis.input_fingerprint
            session.add(
                CompanyDocument(
                    ticker="X",
                    document_date=date.today(),
                    document_type="filing",
                    title="Document 2",
                    text="Text 2",
                )
            )
            session.commit()
        assert service.ensure_all() == 1
        assert provider.calls == 2
        assert service.ensure_all() == 0
        assert provider.calls == 2
        with Session(engine) as session:
            session.delete(session.query(CompanyDocument).filter_by(title="Document 2").one())
            session.commit()
        assert service.ensure_all() == 1
        assert provider.calls == 3
    finally:
        engine.dispose()


def test_derived_theme_synchronization_preserves_other_sources(tmp_path):
    from alpha_lab.themes import ThemeEvidence

    engine = make_engine(f"sqlite:///{tmp_path / 'themes.db'}")
    try:
        create_schema(engine)
        with Session(engine) as session:
            session.add(Security(ticker="X"))
            session.add(
                BusinessTheme(
                    ticker="X", theme="manual", confidence=1, source="curated"
                )
            )
            session.commit()
        repository = Phase3Repository(engine)
        repository.save_themes(
            "X",
            [ThemeEvidence(theme="cloud", confidence=.9, evidence="cloud", source="auto:old")],
            source="auto:old",
            source_prefix="auto:",
        )
        repository.save_themes(
            "X",
            [ThemeEvidence(theme="robotics", confidence=.9, evidence="robotics", source="auto:new")],
            source="auto:new",
            source_prefix="auto:",
        )
        with Session(engine) as session:
            themes = set(session.scalars(select(BusinessTheme.theme)))
        assert themes == {"manual", "robotics"}
    finally:
        engine.dispose()
