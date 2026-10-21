"""The protocol-independent call path: run a tool, then log the call.

The MCP adapter (and any later transport) is a thin shell around
`AgentService.handle`, so everything that matters - the read-only tools, the
envelope, the audit log - is the same and testable without an MCP library.
"""

import logging
import time
from pathlib import Path
from typing import Any

from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.tools import AgentContext, call_tool

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, context: AgentContext, log: CallLog | None = None):
        self.context = context
        self.log = log

    def handle(self, name: str, arguments: dict[str, Any] | None = None, *, client: str | None = None) -> dict[str, Any]:
        """Returns the envelope. A failure to write the audit row is logged to
        stderr and never changes or withholds the answer: a call missing from
        the log can only make a later citation check stricter, not weaker."""
        started = time.perf_counter()
        envelope = call_tool(self.context, name, arguments)
        duration_ms = (time.perf_counter() - started) * 1000
        if self.log is not None:
            try:
                self.log.record(
                    tool=name, arguments=arguments, envelope=envelope, duration_ms=duration_ms, client=client
                )
            except Exception:
                logger.exception("could not write the agent call log; the call was answered but is not recorded")
        return envelope


def default_log_path() -> Path:
    return Path("data") / "agent_calls.db"
