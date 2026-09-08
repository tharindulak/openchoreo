# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The persisted record: the model's words, the classification AE derived, and the
facts the receiver answered.

The split is the point. `rationale` and `related_issues` are the model's and are
copied verbatim. `classification` is read back from AE. Everything about the issue is
stamped from the wire, because the console's Alerts list serves it as-is — a
model restating one loosely would show a human the wrong state mid-incident.
"""

from src.agent.handoff_provider import HandoffProvider
from src.models.handoff_result import (
    HandoffClassification,
    HandoffResult,
    HandoffSummary,
    RelatedIssue,
)

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

SUMMARY = HandoffSummary(
    rationale="filed; the unbounded input needs a guard",
    related_issues=[RelatedIssue(number=12, url="https://x/12", title="same handler")],
)


def test_the_issue_facts_come_from_the_wire_not_the_model():
    outcome = {
        "called": True,
        "ref": 7,
        "link": "https://x/7",
        "existing": False,
        "facts": {"accepted": True, "attempt": 2},
    }

    record = HandoffResult.compose(SUMMARY, outcome, PROVIDER)

    assert record.created_issue_number == 7
    assert record.created_issue_url == "https://x/7"
    assert record.deduped is False
    # Carried verbatim under the receiver's own names, never interpreted here.
    assert record.provider_facts == {"accepted": True, "attempt": 2}


def test_the_model_keeps_its_own_words():
    record = HandoffResult.compose(SUMMARY, {"classification": "mixed"}, PROVIDER)

    assert record.rationale == SUMMARY.rationale
    assert record.related_issues == SUMMARY.related_issues
    assert record.classification is HandoffClassification.MIXED


def test_an_empty_outcome_means_the_create_tool_was_never_reached():
    record = HandoffResult.compose(SUMMARY, {}, PROVIDER)

    assert record.created_issue_number is None
    assert record.created_issue_url is None
    assert record.deduped is False
    assert record.provider_facts == {}


def test_failed_records_code_level_and_names_the_failure():
    # A stage that threw must not read as a stage that decided. The
    # classification stays truthful so the console does not relabel a code-level
    # incident as nothing-to-do, and the rationale says what actually happened.
    record = HandoffResult.failed(RuntimeError("Skill 'x' not found"))
    assert record.classification is HandoffClassification.CODE_LEVEL
    assert "Skill 'x' not found" in record.rationale
    assert record.created_issue_number is None
    assert record.created_issue_url is None
    assert record.deduped is False
    assert record.related_issues == []
