"""Per-test SQLite isolation helpers."""
import pytest
from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine


@pytest.fixture
def db_session():
    """Rollback every test, including commits made through nested transactions."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()
    engine.dispose()
