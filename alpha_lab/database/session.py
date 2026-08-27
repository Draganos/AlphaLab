"""Engine and transaction helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from alpha_lab.database.models import Base, Fundamental


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url)


def create_schema(engine: Engine) -> None:
    if engine.dialect.name == "sqlite":
        _migrate_legacy_fundamentals(engine)
    Base.metadata.create_all(engine)
    # Phase 1.5 additive migration for databases created by Phase 1. No data is rewritten.
    additions = {
        "prices": {"currency": "VARCHAR(8)", "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
                   "source": "VARCHAR(512)", "ingested_at": "DATETIME"},
        "fundamentals": {"currency": "VARCHAR(8)", "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
                         "source": "VARCHAR(512)", "ingested_at": "DATETIME"},
        "estimates": {"currency": "VARCHAR(8)", "provider": "VARCHAR(64) DEFAULT 'unknown' NOT NULL",
                      "source": "VARCHAR(512)", "ingested_at": "DATETIME"},
        "factor_scores": {"score_version": "VARCHAR(32) DEFAULT 'legacy' NOT NULL",
                          "config_hash": "VARCHAR(64) DEFAULT 'legacy' NOT NULL", "generated_at": "DATETIME"},
    }
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            inspector = inspect(connection)
            for table, columns in additions.items():
                existing = {column["name"] for column in inspector.get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_factor_scores_config_hash ON factor_scores (config_hash)"))


def _migrate_legacy_fundamentals(engine: Engine) -> None:
    """Rebuild the Phase 1 table whose ticker/period uniqueness destroyed revisions."""
    inspector = inspect(engine)
    if "fundamentals" not in inspector.get_table_names():
        return
    unique_sets = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("fundamentals")}
    columns = {item["name"] for item in inspector.get_columns("fundamentals")}
    if ("ticker", "period") not in unique_sets and "observation_hash" in columns:
        return
    with engine.begin() as connection:
        for index in inspect(connection).get_indexes("fundamentals"):
            connection.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
        connection.execute(text("ALTER TABLE fundamentals RENAME TO fundamentals_phase1_legacy"))
        Fundamental.__table__.create(connection)
        legacy_columns = {item["name"] for item in inspect(connection).get_columns("fundamentals_phase1_legacy")}
        copy_columns = [column.name for column in Fundamental.__table__.columns
                        if column.name in legacy_columns
                        and column.name not in {"observation_hash", "provider", "ingested_at"}]
        insert_columns = [*copy_columns, "provider", "ingested_at", "observation_hash"]
        names = ", ".join(f'"{name}"' for name in insert_columns)
        copied_values = [f'"{name}"' for name in copy_columns]
        provider_value = "COALESCE(\"provider\", 'unknown')" if "provider" in legacy_columns else "'unknown'"
        ingested_at_value = ("COALESCE(\"ingested_at\", CURRENT_TIMESTAMP)"
                             if "ingested_at" in legacy_columns else "CURRENT_TIMESTAMP")
        select_names = ", ".join([
            *copied_values,
            provider_value,
            ingested_at_value,
            "printf('legacy-%d', id)",
        ])
        connection.execute(text(
            f"INSERT INTO fundamentals ({names}) "
            f"SELECT {select_names} FROM fundamentals_phase1_legacy"
        ))
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
