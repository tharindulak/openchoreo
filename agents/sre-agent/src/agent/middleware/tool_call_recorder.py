# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Records every tool call the handoff stage makes, in order, uninterpreted.

Replaces a middleware that force-injected an argument and parsed the answer
into a receiver's own field names. This one knows no receiver's vocabulary at
all: which tool call's answer matters is not decided here, because the
handoff skill's own constraint ("Creating that issue is your only write")
already guarantees the run's LAST tool call is the one that matters — the
caller reads ``calls[-1]``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from src.agent.tool_result import parse_tool_result


class ToolCallRecorder(AgentMiddleware):
    """Appends ``{"tool": name, "result": parsed}`` for every tool call."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        super().__init__()
        self._calls = calls

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        content = getattr(result, "content", None)
        self._calls.append(
            {"tool": request.tool_call.get("name"), "result": parse_tool_result(content)}
        )
        return result
