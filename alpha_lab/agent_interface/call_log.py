"""Append-only record of every call an agent makes through the interface.

A separate SQLite file, never the research database: the research database is
opened read-only by the interface and must stay that way, and an audit trail
should survive a research-database rebuild. Rows are only ever inserted
(database triggers reject UPDATE and DELETE), so the log is what the agent was
actually told, call by call: the tool, the arguments, and the exact response
(with its hash and the provenance ids it cited). A later check that an agent's
note cites only evidence it really received (roadmap Phase 9, PR C) reads this.

Stdlib only (no MCP, no SQLAlchemy) so it is usable and testable anywhere.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_lab.agent_interface.safety import INTERFACE_VERSION

MAX_LOGGED_ARGUMENT_CHARS = 2_000
MAX_CLIENT_CHARS = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL,
    interface_version TEXT NOT NULL,
    client TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    arguments_truncated INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error_code TEXT,
    ticker TEXT,
    requested_as_of TEXT,
    system_date TEXT,
    provenance_ids TEXT NOT NULL,
    response TEXT NOT NULL,
    response_bytes INTEGER NOT NULL,
    response_sha256 TEXT NOT NULL,
    duration_ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_calls_called_at ON agent_calls (called_at);
CREATE INDEX IF NOT EXISTS ix_agent_calls_tool ON agent_calls (tool);
CREATE TRIGGER IF NOT EXISTS agent_calls_no_update BEFORE UPDATE ON agent_calls
BEGIN SELECT RAISE(ABORT, 'agent_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS agent_calls_no_delete BEFORE DELETE ON agent_calls
BEGIN SELECT RAISE(ABORT, 'agent_calls is append-only'); END;
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


class CallLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the service's worker threads, serialised by a lock.
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def record(
        self,
        *,
        tool: str,
        arguments: Any,
        envelope: dict[str, Any],
        duration_ms: float,
        client: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Append one call and return its row id. `envelope` is the exact
        response dict handed to the agent."""
        argument_text = canonical_json(arguments if arguments is not None else {})
        truncated = len(argument_text) > MAX_LOGGED_ARGUMENT_CHARS
        if truncated:
            argument_text = argument_text[:MAX_LOGGED_ARGUMENT_CHARS]
        response = canonical_json(envelope)
        error = envelope.get("error") or {}
        row = (
            (now or datetime.now(UTC)).isoformat(),
            envelope.get("interface_version", INTERFACE_VERSION),
            None if client is None else client[:MAX_CLIENT_CHARS],
            tool[:200],
            argument_text,
            int(truncated),
            int(bool(envelope.get("ok"))),
            error.get("code"),
            envelope.get("ticker"),
            envelope.get("requested_as_of"),
            envelope.get("system_date"),
            canonical_json(envelope.get("provenance_ids") or []),
            response,
            len(response.encode()),
            hashlib.sha256(response.encode()).hexdigest(),
            round(duration_ms, 3),
        )
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO agent_calls (called_at, interface_version, client, tool, arguments, "
                "arguments_truncated, ok, error_code, ticker, requested_as_of, system_date, "
                "provenance_ids, response, response_bytes, response_sha256, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def entries(self, *, limit: int = 100, tool: str | None = None) -> list[dict[str, Any]]:
        """Newest first; `response` and `provenance_ids` are decoded."""
        query = "SELECT * FROM agent_calls"
        params: list[Any] = []
        if tool is not None:
            query += " WHERE tool = ?"
            params.append(tool)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cursor = self._connection.execute(query, params)
            names = [column[0] for column in cursor.description]
            rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
        for row in rows:
            row["response"] = json.loads(row["response"])
            row["provenance_ids"] = json.loads(row["provenance_ids"])
        return rows

    def close(self) -> None:
        with self._lock:
            self._connection.close()
