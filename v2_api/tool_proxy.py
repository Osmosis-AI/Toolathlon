"""Tool call proxy — forwards client tool calls to the container gateway.

The gateway runs inside the task container and exposes a REST endpoint at
``POST /call-tool``.  This module translates between the v2 API format
and the gateway's format (they are nearly identical).
"""

import httpx

from .models import CallToolResponse
from .session import ExecutionState

TOOL_CALL_TIMEOUT = 300.0  # some tools (e.g. K8s operations) can be slow


async def call_tool(
    execution: ExecutionState,
    tool_name: str,
    arguments: dict,
) -> CallToolResponse:
    """Forward a tool call to the container's gateway and return the result."""
    async with httpx.AsyncClient(timeout=TOOL_CALL_TIMEOUT) as client:
        resp = await client.post(
            f"{execution.gateway_url}/call-tool",
            json={"tool_name": tool_name, "arguments": arguments},
        )

    data = resp.json()

    if resp.status_code == 404:
        return CallToolResponse(
            result=data.get("result", f"Tool not found: {tool_name}"),
            is_error=True,
        )

    if resp.status_code >= 400:
        return CallToolResponse(
            result=data.get("result", f"Gateway error (HTTP {resp.status_code})"),
            is_error=True,
        )

    return CallToolResponse(
        result=data.get("result", ""),
        is_error=data.get("is_error", False),
    )
