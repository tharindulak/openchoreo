# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""What the receiver ANSWERED, recorded from the wire."""

import asyncio
import json

from langchain.messages import ToolMessage

from src.agent.handoff_provider import HandoffProvider
from src.agent.middleware import HandoffOutcomeMiddleware

PROVIDER = HandoffProvider(
    create_issue_tool="tracker_file_defect",
    search_issues_tool="tracker_find_defects",
    header_project="X-Tracker-Project",
    header_component="X-Tracker-Unit",
    header_signature="X-Tracker-Signature",
    answer_issue_number="ref",
    answer_issue_url="link",
    answer_already_filed="existing",
    answer_facts=("accepted", "attempt"),
)
ANSWER = {"ref": 7, "link": "https://x/7", "existing": True, "accepted": True, "attempt": 2}


class _Request:
    def __init__(self, name: str) -> None:
        self.tool_call = {"name": name, "id": "call-1", "args": {}}


def _run(middleware, name, content):
    async def handler(_request):
        return ToolMessage(content=content, tool_call_id="call-1", name=name)
    return asyncio.run(middleware.awrap_tool_call(_Request(name), handler))


def test_a_json_string_answer_is_recorded_under_the_receivers_names():
    outcome: dict = {}
    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.create_issue_tool, json.dumps(ANSWER))
    assert outcome["called"] is True
    assert outcome["ref"] == 7
    assert outcome["link"] == "https://x/7"
    assert outcome["existing"] is True
    assert outcome["facts"] == {"accepted": True, "attempt": 2}


def test_a_content_block_list_is_the_shape_the_real_adapter_returns():
    outcome: dict = {}
    blocks = [{"type": "text", "text": json.dumps(ANSWER), "id": "b1"}]
    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.create_issue_tool, blocks)
    assert outcome["ref"] == 7


def test_an_unreadable_answer_still_records_the_attempt():
    outcome: dict = {}
    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.create_issue_tool, "not json")
    assert outcome["called"] is True
    assert "ref" not in outcome


def test_a_json_value_that_is_not_an_object_still_records_the_attempt():
    outcome: dict = {}

    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.create_issue_tool, "[]")

    assert outcome == {"called": True}


def test_a_fact_the_receiver_did_not_answer_is_absent_rather_than_false():
    outcome: dict = {}
    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.create_issue_tool, json.dumps({"ref": 7}))
    assert outcome["facts"] == {}


def test_another_tool_is_left_alone():
    outcome: dict = {}
    _run(HandoffOutcomeMiddleware(PROVIDER, outcome), PROVIDER.search_issues_tool, json.dumps(ANSWER))
    assert outcome == {}
