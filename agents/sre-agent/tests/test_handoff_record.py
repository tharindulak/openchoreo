# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The persisted record: which tool the handoff stage's last call was, and
whatever it answered — verbatim, uninterpreted. Nothing here knows any
receiver's vocabulary."""

from src.models.rca_report import HandoffResult


def test_compose_carries_the_last_calls_tool_and_result_verbatim():
    record = HandoffResult.compose(
        {"tool": "ae_create_issue", "result": {"number": 7, "deduped": False}}
    )
    assert record.tool == "ae_create_issue"
    assert record.result == {"number": 7, "deduped": False}
    assert record.failure_reason is None


def test_compose_of_none_means_no_tool_call_was_ever_made():
    record = HandoffResult.compose(None)
    assert record.tool is None
    assert record.result is None
    assert record.failure_reason is None


def test_failed_names_the_failure_and_carries_no_tool_or_result():
    record = HandoffResult.failed(RuntimeError("Skill 'x' not found"))
    assert "Skill 'x' not found" in record.failure_reason
    assert record.tool is None
    assert record.result is None
