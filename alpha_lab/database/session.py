"""Engine and transaction helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session

from alpha_lab.database.models import Base, Fundamental

# How long a SQLite connection waits on a lock held by another connection
# before raising "database is locked", instead of failing immediately.
# SQLite allows exactly one writer at a time; this app has several
# concurrent writers in production (the dashboard's own auto-refresh,
# manual Full Refresh, and refresh scripts can all run against the same
# database file), so a brief wait here is the standard mitigation rather
# than surfacing a spurious failure for an ordinary, short-lived overlap.
_SQLITE_BUSY_TIMEOUT_SECONDS = 30


def make_engine(url: str) -> Engine:
    is_file_based_sqlite = url.startswith("sqlite:///") and ":memory:" not in url
    if is_file_based_sqlite:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if is_file_based_sqlite:
        # WAL mode lets readers and the one writer proceed concurrently
        # instead of blocking each other -- :memory: databases don't
        # support it (and don't need it: there is nothing else to
        # contend with a single in-process connection).
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def create_schema(engine: Engine) -> None:
    if engine.dialect.name == "sqlite":
        _migrate_legacy_fundamentals(engine)
    Base.metadata.create_all(engine)
    # Phase 1.5 additive migration for databases created by Phase 1. No data is rewritten.
    additions = {
        "securities": {
            "industry": "VARCHAR(128)",
            "market_cap": "FLOAT",
            "business_description": "TEXT",
            "metadata_provider": "VARCHAR(64)",
            "metadata_source": "VARCHAR(512)",
            "metadata_updated_at": "DATETIME",
        },
        "prices": {
            "currency": "VARCHAR(8)",
            "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
            "source": "VARCHAR(512)",
            "ingested_at": "DATETIME",
        },
        "fundamentals": {
            "currency": "VARCHAR(8)",
            "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
            "source": "VARCHAR(512)",
            "ingested_at": "DATETIME",
            "gross_profit": "FLOAT",
            "total_assets": "FLOAT",
            "current_assets": "FLOAT",
            "current_liabilities": "FLOAT",
            "interest_expense": "FLOAT",
            "dividends_paid": "FLOAT",
            "share_repurchases": "FLOAT",
            "provenance_json": "JSON",
        },
        "estimates": {
            "currency": "VARCHAR(8)",
            "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
            "source": "VARCHAR(512)",
            "ingested_at": "DATETIME",
            "estimate_dispersion": "FLOAT",
            "observation_hash": "VARCHAR(64)",
        },
        "factor_scores": {
            "score_version": "VARCHAR(32) DEFAULT 'legacy' NOT NULL",
            "config_hash": "VARCHAR(64) DEFAULT 'legacy' NOT NULL",
            "generated_at": "DATETIME",
        },
        "ethical_evaluations": {
            "evidence_fingerprint": "VARCHAR(64)",
        },
        "ai_research_analyses": {
            "analyzed_document_ids": "JSON",
            "input_fingerprint": "VARCHAR(64)",
        },
    }
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            inspector = inspect(connection)
            for table, columns in additions.items():
                existing = {column["name"] for column in inspector.get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(
                            text(
                                f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
                            )
                        )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_factor_scores_config_hash ON factor_scores (config_hash)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_estimates_observation_hash "
                    "ON estimates (observation_hash) WHERE observation_hash IS NOT NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_ai_research_input_fingerprint "
                    "ON ai_research_analyses (input_fingerprint)"
                )
            )


def _migrate_legacy_fundamentals(engine: Engine) -> None:
    """Rebuild the Phase 1 table whose ticker/period uniqueness destroyed revisions."""
    inspector = inspect(engine)
    if "fundamentals" not in inspector.get_table_names():
        return
    unique_sets = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("fundamentals")
    }
    columns = {item["name"] for item in inspector.get_columns("fundamentals")}
    if ("ticker", "period") not in unique_sets and "observation_hash" in columns:
        return
    with engine.begin() as connection:
        for index in inspect(connection).get_indexes("fundamentals"):
            connection.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
        connection.execute(
            text("ALTER TABLE fundamentals RENAME TO fundamentals_phase1_legacy")
        )
        Fundamental.__table__.create(connection)
        legacy_columns = {
            item["name"]
            for item in inspect(connection).get_columns("fundamentals_phase1_legacy")
        }
        copy_columns = [
            column.name
            for column in Fundamental.__table__.columns
            if column.name in legacy_columns
            and column.name not in {"observation_hash", "provider", "ingested_at"}
        ]
        insert_columns = [*copy_columns, "provider", "ingested_at", "observation_hash"]
        names = ", ".join(f'"{name}"' for name in insert_columns)
        copied_values = [f'"{name}"' for name in copy_columns]
        provider_value = (
            "COALESCE(\"provider\", 'unknown')"
            if "provider" in legacy_columns
            else "'unknown'"
        )
        ingested_at_value = (
            'COALESCE("ingested_at", CURRENT_TIMESTAMP)'
            if "ingested_at" in legacy_columns
            else "CURRENT_TIMESTAMP"
        )
        select_names = ", ".join(
            [
                *copied_values,
                provider_value,
                ingested_at_value,
                "printf('legacy-%d', id)",
            ]
        )
        connection.execute(
            text(
                f"INSERT INTO fundamentals ({names}) "
                f"SELECT {select_names} FROM fundamentals_phase1_legacy"
            )
        )
        connection.execute(text("DROP TABLE fundamentals_phase1_legacy"))


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
