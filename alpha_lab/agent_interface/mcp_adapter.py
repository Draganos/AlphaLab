"""MCP (Model Context Protocol) adapter for the Agent Research Interface.

A thin shell around `AgentService`: it lists the registry's read-only tools and
routes each call through `AgentService.handle`, so the envelope, safety rules
and audit log are identical to every other transport. The `mcp` package is an
optional extra (`pip install "alpha-lab[mcp]"`) outside the locked CI
dependencies; this module is the only place that imports it, and nothing else
imports this module.

stdout is the protocol channel: nothing here (or in the tools) may print to it.
"""

from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server

from alpha_lab.agent_interface.call_log import canonical_json
from alpha_lab.agent_interface.service import AgentService
from alpha_lab.agent_interface.tools import USAGE_CONTRACT, tool_catalog

SERVER_NAME = "alphalab-research"
INSTRUCTIONS = (
    "Read-only AlphaLab research evidence. Not investment advice. "
    + " ".join(USAGE_CONTRACT)
)


def _client_name(server: Server) -> str | None:
    try:
        return server.request_context.session.client_params.clientInfo.name
    except (AttributeError, LookupError):  # no active request, or a client that sent no info
        return None


def build_server(service: AgentService) -> Server:
    server: Server = Server(SERVER_NAME, instructions=INSTRUCTIONS)
    annotations = types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=entry["input_schema"],
                annotations=annotations,
            )
            for entry in tool_catalog()
        ]

    # Input is validated by the tool's own argument model (which returns a
    # structured, logged `invalid_arguments` envelope), not by the SDK, so a
    # malformed call is still recorded in the audit log.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.ContentBlock]:
        client = _client_name(server)
        envelope = await anyio.to_thread.run_sync(lambda: service.handle(name, arguments, client=client))
        # Byte-identical to what the call log hashes, so a response can be
        # verified against its log row.
        return [types.TextContent(type="text", text=canonical_json(envelope))]

    return server


async def serve_stdio(service: AgentService) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
