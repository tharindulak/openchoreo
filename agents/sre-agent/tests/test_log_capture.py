# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""LogCaptureMiddleware: what it captures, what it leaves alone, and that it
never changes what the model sees."""

import asyncio
import json

from langchain.messages import ToolMessage

from src.agent.middleware import LogCaptureMiddleware
from src.agent.tool_registry import TOOLS

LOGS_RESULT = {
    "logs": [
        {"timestamp": "t1", "level": "ERROR", "log": "boom", "metadata": {"componentName": "c"}},
        {"timestamp": "t2", "level": "INFO", "log": "ok", "metadata": {"componentName": "c"}},
    ]
}


class _Request:
    def __init__(self, name: str) -> None:
        self.tool_call = {"name": name, "id": "call-1", "args": {}}


def _run(middleware, name, content):
    async def handler(_request):
        return ToolMessage(content=content, tool_call_id="call-1", name=name)

    return asyncio.run(middleware.awrap_tool_call(_Request(name), handler))


def test_captures_the_raw_logs_from_a_json_string_result():
    captured: list[dict] = []
    _run(LogCaptureMiddleware(captured), TOOLS.QUERY_COMPONENT_LOGS, json.dumps(LOGS_RESULT))
    assert captured == LOGS_RESULT["logs"]


def test_captures_from_a_content_block_list_too():
    captured: list[dict] = []
    blocks = [{"type": "text", "text": json.dumps(LOGS_RESULT), "id": "b1"}]
    _run(LogCaptureMiddleware(captured), TOOLS.QUERY_COMPONENT_LOGS, blocks)
    assert captured == LOGS_RESULT["logs"]


def test_accumulates_across_multiple_calls_in_one_run():
    captured: list[dict] = []
    middleware = LogCaptureMiddleware(captured)
    _run(middleware, TOOLS.QUERY_COMPONENT_LOGS, json.dumps(LOGS_RESULT))
    _run(middleware, TOOLS.QUERY_COMPONENT_LOGS, json.dumps({"logs": [{"log": "second call"}]}))
    assert len(captured) == 3


def test_ignores_other_tools():
    captured: list[dict] = []
    _run(LogCaptureMiddleware(captured), TOOLS.QUERY_RESOURCE_METRICS, json.dumps(LOGS_RESULT))
    assert captured == []


def test_an_unreadable_result_is_skipped_not_raised():
    captured: list[dict] = []
    # Must not raise — a malformed logs payload must not take down the RCA run.
    _run(LogCaptureMiddleware(captured), TOOLS.QUERY_COMPONENT_LOGS, "not json")
    assert captured == []


def test_the_model_still_sees_the_result_unchanged():
    captured: list[dict] = []

    async def handler(_request):
        return ToolMessage(content=json.dumps(LOGS_RESULT), tool_call_id="call-1", name="x")

    result = asyncio.run(
        LogCaptureMiddleware(captured).awrap_tool_call(
            _Request(TOOLS.QUERY_COMPONENT_LOGS), handler
        )
    )
    assert result.content == json.dumps(LOGS_RESULT)
