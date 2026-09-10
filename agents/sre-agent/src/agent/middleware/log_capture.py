# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Captures what query_component_logs actually returned, unmodified.

The dedupe fingerprint needs a source that is the same across two runs of the
same underlying error. The RCA model's own structured findings are not that
source — which lines it chooses to cite in `supporting_findings[].evidence`
varies run to run the way any model output varies, so two identical incidents
can fingerprint differently. What the observability plane actually returned
for a `query_component_logs` call does not vary that way; this middleware is
how the fingerprint gets to use it instead.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from src.agent.tool_registry import TOOLS
from src.agent.tool_result import parse_tool_result

logger = logging.getLogger(__name__)


class LogCaptureMiddleware(AgentMiddleware):
    """Appends every `query_component_logs` call's raw log entries to a list
    the caller owns, in addition to (never instead of) what the model sees —
    `OutputTransformerMiddleware` still renders the model's own copy.
    """

    def __init__(self, captured: list[dict[str, Any]]) -> None:
        super().__init__()
        self._captured = captured

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if request.tool_call.get("name") != TOOLS.QUERY_COMPONENT_LOGS:
            return result
        if not isinstance(result, ToolMessage):
            return result
        content = parse_tool_result(result.content)
        logs = content.get("logs") if isinstance(content, dict) else None
        if isinstance(logs, list):
            self._captured.extend(entry for entry in logs if isinstance(entry, dict))
        return result
