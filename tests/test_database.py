import threading
import time
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
from alpha_lab.database.session import _SQLITE_BUSY_TIMEOUT_SECONDS
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


# --- concurrent-write reliability: real production finding ------------
# "database is locked" was observed live on the automatic stale-ticker
# refresh (UPDATE securities ... for BRK.B). Root cause: make_engine used
# SQLite's default connection (a 5-second busy timeout, journal mode
# DELETE), which fails immediately once two writers overlap for longer
# than that -- an ordinary occurrence once several refresh paths
# (automatic on-session-start, manual Full Refresh, batch scripts) can
# all touch the same database file. Fixed with a longer busy timeout and
# WAL mode, which lets a writer wait instead of failing.


def test_make_engine_configures_a_busy_timeout_and_wal_mode_for_file_based_sqlite(tmp_path):
    db_path = tmp_path / "pragmas.db"
    engine = make_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        busy_timeout_ms = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert busy_timeout_ms == _SQLITE_BUSY_TIMEOUT_SECONDS * 1000
    assert journal_mode == "wal"
    engine.dispose()


def test_make_engine_does_not_apply_wal_mode_to_an_in_memory_database():
    """:memory: databases don't support WAL (nothing else could ever
    contend with their single in-process connection anyway) -- make_engine
    must not attempt to set it there."""
    engine = make_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert journal_mode != "wal"
    engine.dispose()


def test_a_writer_holding_the_lock_past_sqlites_old_default_timeout_no_longer_fails(tmp_path):
    """Regression test reproducing the exact reported failure: a second
    writer that must wait for a slow first writer must wait (up to the
    configured busy timeout), not fail immediately. Holds the lock for 6
    seconds -- longer than SQLite's old 5-second default timeout (which
    this exact scenario was confirmed, live, to still raise
    `sqlite3.OperationalError: database is locked` against), well under
    the new `_SQLITE_BUSY_TIMEOUT_SECONDS` (30s) this fix configures."""
    db_path = tmp_path / "concurrent.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)

    first = engine.raw_connection()
    first.driver_connection.isolation_level = None
    first.execute("BEGIN IMMEDIATE")
    first.execute("INSERT INTO securities (ticker, country, currency) VALUES ('LOCKED', 'US', 'USD')")

    second_writer_errors: list[Exception] = []

    def _second_writer() -> None:
        try:
            connection = engine.raw_connection()
            connection.driver_connection.isolation_level = None
            connection.execute(
                "INSERT INTO securities (ticker, country, currency) VALUES ('OTHER', 'US', 'USD')"
            )
            connection.commit()
            connection.close()
        except Exception as error:  # noqa: BLE001 - captured for the assertion below
            second_writer_errors.append(error)

    thread = threading.Thread(target=_second_writer)
    thread.start()
    time.sleep(6)
    first.commit()
    first.close()
    thread.join(timeout=15)

    assert second_writer_errors == []
    with Session(engine) as session:
        tickers = {row.ticker for row in session.scalars(select(Security))}
    assert tickers == {"LOCKED", "OTHER"}
    engine.dispose()


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
