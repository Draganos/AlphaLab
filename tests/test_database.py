from datetime import date
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from alpha_lab.database.models import (
    Base,
    CurrentAIResearchAssessment,
    CurrentAnalystConsensus,
    CurrentTechnicalSummary,
    CurrentExternalCalibration,
    Estimate,
    ExternalCalibrationSnapshot,
    Fundamental,
    Price,
    Security,
)
from alpha_lab.database import create_schema, make_engine
from sqlalchemy import inspect


def test_database_crud(db_session: Session):
    db_session.add(Security(ticker="TEST", company_name="Test", country="US", currency="USD"))
    db_session.add(Price(ticker="TEST", date=date(2024, 1, 1), close=10, adjusted_close=10,
                         provider="test-fixture", currency="USD", source="unit test"))
    db_session.commit()
    price = db_session.scalar(select(Price).where(Price.ticker == "TEST"))
    assert price.close == 10
    assert price.provider == "test-fixture"
    assert price.ingested_at is not None


def test_schema_initialization_is_idempotent():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    create_schema(engine)
    assert "provider" in {column["name"] for column in inspect(engine).get_columns("prices")}


def test_schema_initialization_creates_research_snapshots_table_idempotently():
    """New table added for PR #15 historical research persistence; a plain
    Base.metadata.create_all is sufficient here since nothing pre-existing
    is altered, but repeated init must still be safe."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    create_schema(engine)
    assert "research_snapshots" in inspect(engine).get_table_names()
    columns = {column["name"] for column in inspect(engine).get_columns("research_snapshots")}
    assert {
        "snapshot_id", "ticker", "evaluation_date", "generated_at",
        "rating_version", "configuration_hash", "research_schema_version",
        "payload_hash", "payload",
    } <= columns


def test_point_in_time_fields_are_separate(db_session: Session):
    db_session.add(Security(ticker="ASOF", currency="USD"))
    db_session.add(Fundamental(ticker="ASOF", period=date(2024, 3, 31),
                              publication_date=date(2024, 5, 10), provider="fixture",
                              observation_hash="asof-fundamental-v1"))
    db_session.add(Estimate(ticker="ASOF", observation_date=date(2024, 4, 1),
                            fiscal_period=date(2024, 6, 30), consensus_eps=1.5, provider="fixture"))
    db_session.flush()
    fundamental = db_session.scalar(select(Fundamental))
    estimate = db_session.scalar(select(Estimate))
    assert fundamental.period != fundamental.publication_date
    assert estimate.observation_date != estimate.fiscal_period


def test_create_schema_upgrades_a_pre_supplemental_database_without_data_loss():
    """Reproduces the reported crash: a database created before the
    Analyst Consensus / Technical Summary / AI Research / External
    Calibration ("Current*") tables existed must upgrade in place when
    `create_schema` runs again -- never require deleting/recreating the
    database, and never lose data already in it.

    Simulates the "old" database by creating only the tables that existed
    before those additions (everything in Base.metadata minus the five
    newer tables), inserting a row into a pre-existing table, then running
    `create_schema` exactly as every ingestion script and (as of this fix)
    every Streamlit page already does on startup.
    """
    newer_tables = {
        CurrentAnalystConsensus.__tablename__,
        CurrentTechnicalSummary.__tablename__,
        CurrentAIResearchAssessment.__tablename__,
        CurrentExternalCalibration.__tablename__,
        ExternalCalibrationSnapshot.__tablename__,
    }
    older_tables = [
        table for name, table in Base.metadata.tables.items() if name not in newer_tables
    ]

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=older_tables)
    existing_before = set(inspect(engine).get_table_names())
    assert not (newer_tables & existing_before), "test setup must omit the newer tables"

    with Session(engine) as session:
        session.add(Security(ticker="OLD", company_name="Pre-existing Co", currency="USD"))
        session.commit()

    # This is exactly what a Streamlit page (and every batch script) does on
    # startup -- must not require deleting or recreating the database file.
    create_schema(engine)
    create_schema(engine)  # idempotent: safe to run more than once

    existing_after = set(inspect(engine).get_table_names())
    assert newer_tables <= existing_after

    with Session(engine) as session:
        preserved = session.scalar(select(Security).where(Security.ticker == "OLD"))
        assert preserved is not None
        assert preserved.company_name == "Pre-existing Co"


def test_legacy_fundamental_constraint_is_migrated_without_data_loss():
    engine = make_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE fundamentals (
                id INTEGER PRIMARY KEY, ticker VARCHAR(32) NOT NULL, period DATE NOT NULL,
                publication_date DATE, eps FLOAT,
                CONSTRAINT legacy_unique UNIQUE (ticker, period)
            )
        """))
        connection.execute(text("""
            INSERT INTO fundamentals (id, ticker, period, publication_date, eps)
            VALUES (1, 'LEGACY', '2023-12-31', '2024-02-01', 1.0)
        """))
    create_schema(engine)
    create_schema(engine)
    with Session(engine) as session:
        migrated = session.scalars(select(Fundamental)).all()
        assert len(migrated) == 1
        migrated = migrated[0]
        assert migrated.eps == 1.0
        assert migrated.provider == "unknown"
        assert migrated.observation_hash == "legacy-1"
        assert migrated.ingested_at is not None
