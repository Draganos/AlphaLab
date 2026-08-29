from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import EthicalEvaluation, Security
from alpha_lab.ethics import EthicalClassificationService, load_ethics_policy


def test_automatic_classification_persists_and_reevaluates_metadata(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ethics.db'}")
    try:
        create_schema(engine)
        with Session(engine) as session:
            session.add(
                Security(
                    ticker="AUTO",
                    sector="Financials",
                    industry="Payment Processing",
                    business_description="Global payment network infrastructure",
                    metadata_source="fixture-v1",
                )
            )
            session.commit()
        service = EthicalClassificationService(engine, load_ethics_policy())
        assert service.ensure_all()["AUTO"] == "PASS"
        service.ensure_all()
        with Session(engine) as session:
            assert session.scalar(select(func.count(EthicalEvaluation.id))) == 1
            security = session.get(Security, "AUTO")
            security.business_description = (
                "Bank holding company accepting deposits and providing loans"
            )
            security.industry = "Banks"
            session.commit()
        assert service.ensure_all()["AUTO"] == "EXCLUDED"
        changed_policy = load_ethics_policy().model_copy(
            update={"manual_exclude": ["AUTO"]}
        )
        EthicalClassificationService(engine, changed_policy).ensure_all()
        with Session(engine) as session:
            records = list(
                session.scalars(
                    select(EthicalEvaluation).order_by(EthicalEvaluation.id)
                )
            )
            assert len(records) == 3
            assert records[0].evidence_fingerprint != records[1].evidence_fingerprint
            assert records[1].policy_version != records[2].policy_version
    finally:
        engine.dispose()
