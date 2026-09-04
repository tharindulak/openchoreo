# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Records the receiving platform's answer to a create-issue call."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from src.agent.handoff_provider import HandoffProvider

logger = logging.getLogger(__name__)


def parse_tool_result(raw: Any) -> dict[str, Any]:
    """Normalize a tool result content value into a dictionary."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"tool result JSON is not an object: {parsed!r}")
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                parsed = json.loads(block.get("text", ""))
                if isinstance(parsed, dict):
                    return parsed
                raise ValueError(f"tool result JSON is not an object: {parsed!r}")
        raise ValueError(f"no text content block in tool result: {raw!r}")
    raise TypeError(f"unexpected tool result type {type(raw)!r}: {raw!r}")


class HandoffOutcomeMiddleware(AgentMiddleware):
    """Observe the create-issue tool and record its wire response."""

    def __init__(self, provider: HandoffProvider, outcome: dict[str, Any]) -> None:
        super().__init__()
        self._provider = provider
        self._outcome = outcome

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != self._provider.create_issue_tool:
            return await handler(request)
        self._outcome["called"] = True
        result = await handler(request)
        self._record(getattr(result, "content", None))
        return result

    def _record(self, content: Any) -> None:
        provider = self._provider
        try:
            answer = parse_tool_result(content)
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            logger.warning("Could not parse %s result: %r", provider.create_issue_tool, content)
            return
        self._outcome.update(
            {
                provider.answer_issue_number: answer.get(provider.answer_issue_number),
                provider.answer_issue_url: answer.get(provider.answer_issue_url),
                provider.answer_already_filed: bool(answer.get(provider.answer_already_filed)),
                "facts": {
                    name: answer[name] for name in provider.answer_facts if name in answer
                },
            }
        )
