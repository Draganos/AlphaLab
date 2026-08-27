"""Engine and transaction helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from alpha_lab.database.models import Base


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url)


def create_schema(engine: Engine) -> None:
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
