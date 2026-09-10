# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""ToolCallRecorder and parse_tool_result: a fully generic record of what a
tool call answered, with no knowledge of any particular tool's name or shape.
"""

import asyncio
import json

from langchain.messages import ToolMessage

from src.agent.middleware import ToolCallRecorder
from src.agent.tool_result import parse_tool_result


class _Request:
    def __init__(self, name: str) -> None:
        self.tool_call = {"name": name, "id": "call-1", "args": {}}


def _run(middleware, name, content):
    async def handler(_request):
        return ToolMessage(content=content, tool_call_id="call-1", name=name)

    return asyncio.run(middleware.awrap_tool_call(_Request(name), handler))


def test_parse_tool_result_decodes_a_json_string():
    assert parse_tool_result(json.dumps({"a": 1})) == {"a": 1}


def test_parse_tool_result_decodes_a_json_array_not_just_objects():
    # ae_search_related_issues answers a JSON array, not an object — a parser
    # that only accepted objects would raise on exactly the call this recorder
    # must also handle generically.
    assert parse_tool_result(json.dumps([1, 2, 3])) == [1, 2, 3]


def test_parse_tool_result_decodes_a_content_block_list():
    blocks = [{"type": "text", "text": json.dumps({"a": 1}), "id": "b1"}]
    assert parse_tool_result(blocks) == {"a": 1}


def test_parse_tool_result_never_raises_on_unparseable_input():
    assert parse_tool_result("not json") == "not json"
    assert parse_tool_result(None) is None


def test_recorder_appends_every_call_tool_and_parsed_result_in_order():
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls)
    _run(recorder, "ae_search_related_issues", json.dumps([{"number": 1}]))
    _run(recorder, "ae_create_issue", json.dumps({"number": 2, "deduped": False}))

    assert calls == [
        {"tool": "ae_search_related_issues", "result": [{"number": 1}]},
        {"tool": "ae_create_issue", "result": {"number": 2, "deduped": False}},
    ]


def test_recorder_does_not_filter_by_tool_name():
    # The whole point: it does not know or care which tool "matters".
    calls: list[dict] = []
    _run(ToolCallRecorder(calls), "load_skill", "some skill body")
    assert calls == [{"tool": "load_skill", "result": "some skill body"}]
