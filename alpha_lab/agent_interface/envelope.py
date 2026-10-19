"""The one response envelope every tool uses.

An agent always receives the same shape: what was asked (``tool``,
``requested_as_of``), what day the system believes it is (``system_date``,
needed to reason about freshness), the payload (``data``), every provenance id
cited in it, per-answer freshness, plain-language ``limitations``, and a
structured ``error`` instead of an exception. Missing data stays ``null`` with
a status in ``data``; nothing is defaulted to zero or guessed.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from alpha_lab.agent_interface.safety import INTERFACE_VERSION


class AgentToolError(Exception):
    """A request the interface refuses, with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ToolError(BaseModel):
    code: str
    message: str


class ToolEnvelope(BaseModel):
    interface_version: str = INTERFACE_VERSION
    tool: str
    ok: bool
    system_date: date
    requested_as_of: date | None = None
    ticker: str | None = None
    data: Any = None
    provenance_ids: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: ToolError | None = None


class ToolResult(BaseModel):
    """What a handler returns; the registry wraps it into a `ToolEnvelope`."""

    data: Any
    ticker: str | None = None
    requested_as_of: date | None = None
    freshness: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    # True only for answers made wholly of AlphaLab's own static text (the
    # methodology): they are cleaned but not labelled untrusted.
    trusted: bool = False
