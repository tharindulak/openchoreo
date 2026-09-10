# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Records tool calls the handoff stage makes against the receiving platform,
in order, uninterpreted.

Replaces a middleware that force-injected an argument and parsed the answer
into a receiver's own field names. This one knows no receiver's vocabulary at
all: which tool call's answer matters is not decided here, because the
handoff skill's own constraint ("Creating that issue is your only write")
already guarantees the run's LAST tool call is the one that matters — the
caller reads ``calls[-1]``.

It is scoped to tools discovered from the named MCP connection, not every
tool the model can call. The agent also carries local, non-receiver tools —
``load_skill`` chief among them — and a run that never reaches the receiver
(the model gives up, an error is swallowed, it just answers in prose) would
otherwise leave ``calls[-1]`` as a ``load_skill`` call whose "result" is a
whole SKILL.md body. That would be indistinguishable from a real, completed
handoff to any caller reading the last entry. Scoping to the connection's own
tool names — generic, not any particular tool's name — keeps that invariant:
an unfiled run has an empty (or receiver-only-but-non-terminal) call list.
"""

from collections.abc import Awaitable, Callable, Collection
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from src.agent.tool_result import parse_tool_result


class ToolCallRecorder(AgentMiddleware):
    """Appends ``{"tool": name, "result": parsed}`` for each call to a tool in
    ``recordable_tool_names`` — silently skipping any other tool call."""

    def __init__(
        self, calls: list[dict[str, Any]], recordable_tool_names: Collection[str]
    ) -> None:
        super().__init__()
        self._calls = calls
        self._recordable_tool_names = frozenset(recordable_tool_names)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        name = request.tool_call.get("name")
        if name in self._recordable_tool_names:
            content = getattr(result, "content", None)
            self._calls.append({"tool": name, "result": parse_tool_result(content)})
        return result
