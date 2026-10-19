"""A database connection that physically cannot write.

The Agent Research Interface only ever reads. Rather than rely on every tool
being correct, the engine itself is opened read-only: SQLite rejects any write
("attempt to write a readonly database"), and ``query_only`` is set as a second
guard. Callers must therefore never run ``create_schema`` on it.
"""

from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event


def make_readonly_engine(database_url: str) -> Engine:
    """Open the SQLite file behind ``database_url`` read-only."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or ":memory:" in database_url:
        raise ValueError("the agent interface needs a file-backed SQLite database (sqlite:///path/to.db)")
    path = Path(database_url.removeprefix(prefix)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database file not found: {path}")
    posix = path.as_posix()
    uri = f"file://{'' if posix.startswith('/') else '/'}{quote(posix)}?mode=ro"
    engine = create_engine(f"sqlite:///{uri}&uri=true")

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    return engine
