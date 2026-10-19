"""Agent Research Interface (roadmap Phase 9): a read-only, point-in-time,
citeable view of AlphaLab research for an external agent. See `tools` for the
tool layer and its gates. Nothing in scoring, ranking, backtesting or
portfolio code imports this package."""

from alpha_lab.agent_interface.envelope import AgentToolError, ToolEnvelope
from alpha_lab.agent_interface.readonly import make_readonly_engine
from alpha_lab.agent_interface.safety import (
    INTERFACE_VERSION,
    sanitize_value,
    wrap_untrusted,
)
from alpha_lab.agent_interface.tools import TOOLS, AgentContext, call_tool, tool_catalog

__all__ = [
    "INTERFACE_VERSION",
    "TOOLS",
    "AgentContext",
    "AgentToolError",
    "ToolEnvelope",
    "call_tool",
    "make_readonly_engine",
    "sanitize_value",
    "tool_catalog",
    "wrap_untrusted",
]
