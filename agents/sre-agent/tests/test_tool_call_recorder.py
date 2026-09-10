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


# A realistic server-discovered tool-name set, the same shape agent.py builds
# from `mcp_client.get_tools(server_name=...)` — used to prove the recorder
# scopes to it rather than recording everything.
_HANDOFF_SERVER_TOOLS = frozenset({"ae_search_related_issues", "ae_create_issue"})


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


def test_recorder_appends_every_recordable_call_tool_and_parsed_result_in_order():
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls, _HANDOFF_SERVER_TOOLS)
    _run(recorder, "ae_search_related_issues", json.dumps([{"number": 1}]))
    _run(recorder, "ae_create_issue", json.dumps({"number": 2, "deduped": False}))

    assert calls == [
        {"tool": "ae_search_related_issues", "result": [{"number": 1}]},
        {"tool": "ae_create_issue", "result": {"number": 2, "deduped": False}},
    ]


def test_recorder_does_not_filter_within_the_recordable_set():
    # Among tools that DID come from the named connection, the recorder still
    # does not know or care which one "matters" — that is left to the caller
    # reading calls[-1].
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls, _HANDOFF_SERVER_TOOLS)
    _run(recorder, "ae_search_related_issues", "some result")
    assert calls == [{"tool": "ae_search_related_issues", "result": "some result"}]


def test_recorder_ignores_a_local_tool_not_from_the_named_connection():
    # load_skill is this agent's own local tool (added via
    # create_load_skill_tool), never something the handoff MCP connection
    # advertised. A run that only calls load_skill — never reaching the
    # receiver — must not leave a recorded entry that looks like a completed
    # handoff.
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls, _HANDOFF_SERVER_TOOLS)
    _run(recorder, "load_skill", "the entire SKILL.md body")
    assert calls == []


def test_recorder_records_nothing_when_the_recordable_set_is_empty():
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls, frozenset())
    _run(recorder, "ae_create_issue", json.dumps({"number": 1}))
    assert calls == []


def test_recorder_only_records_the_server_tool_amid_a_mixed_run():
    # The scenario the finding describes: the model calls load_skill first
    # (per the handoff prompt), then a server tool. Only the latter lands.
    calls: list[dict] = []
    recorder = ToolCallRecorder(calls, _HANDOFF_SERVER_TOOLS)
    _run(recorder, "load_skill", "the entire SKILL.md body")
    _run(recorder, "ae_create_issue", json.dumps({"number": 2, "deduped": False}))
    assert calls == [{"tool": "ae_create_issue", "result": {"number": 2, "deduped": False}}]
