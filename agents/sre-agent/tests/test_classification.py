# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Reading the classification back, now that AE derives it.

What the remediation statuses MEAN moved to AE: it is the same rule AE's own
escalation reads, and two derivations of one fact are two things that can
disagree. This stage sends the statuses and records the answer, so what is left
to test here is the read-back — the spelling boundary, and what gets recorded
when there is no answer at all.
"""

from src.agent.handoff_provider import HandoffProvider
from src.models.handoff_result import (
    HandoffClassification,
    HandoffResult,
    HandoffSummary,
)

PROVIDER = HandoffProvider(
    create_issue_tool="ae_create_issue",
    search_issues_tool="ae_search_related_issues",
    header_project="X-AEP-Incident-Project",
    header_component="X-AEP-Incident-Component",
    header_signature="X-AEP-Incident-Signature",
)

SUMMARY = HandoffSummary(rationale="filed")


def _compose(outcome: dict) -> HandoffClassification:
    return HandoffResult.compose(SUMMARY, outcome, PROVIDER).classification


def test_every_classification_survives_the_spelling_boundary():
    # AE spells them with hyphens, this enum with underscores. A value that
    # failed to translate would land on the code_level default and look like a
    # deliberate answer.
    assert _compose({"classification": "code-level"}) is HandoffClassification.CODE_LEVEL
    assert _compose({"classification": "config-level"}) is HandoffClassification.CONFIG_LEVEL
    assert _compose({"classification": "mixed"}) is HandoffClassification.MIXED
    assert _compose({"classification": "none"}) is HandoffClassification.NONE


def test_the_answer_name_comes_from_the_descriptor():
    provider = HandoffProvider(
        create_issue_tool="tracker_file_defect",
        search_issues_tool="tracker_find_defects",
        header_project="X-Tracker-Project",
        header_component="X-Tracker-Unit",
        header_signature="X-Tracker-Signature",
        answer_classification="workKind",
    )
    record = HandoffResult.compose(SUMMARY, {"workKind": "config-level"}, provider)
    assert record.classification is HandoffClassification.CONFIG_LEVEL


def test_an_unanswered_call_records_code_level():
    # The stage threw, the model ended its turn without calling create, or the
    # receiver said something unrecognised. The read side defaults an absent
    # value to `none`, which would relabel a real code-level incident as
    # nothing-to-do on the report somebody triages — so the safe direction is
    # recorded instead.
    assert _compose({}) is HandoffClassification.CODE_LEVEL
    assert _compose({"classification": ""}) is HandoffClassification.CODE_LEVEL
    assert _compose({"classification": "not-a-classification"}) is HandoffClassification.CODE_LEVEL
    assert _compose({"classification": 7}) is HandoffClassification.CODE_LEVEL


def test_a_stage_that_threw_records_code_level_too():
    record = HandoffResult.failed(RuntimeError("boom"))
    assert record.classification is HandoffClassification.CODE_LEVEL
    assert "boom" in record.rationale
