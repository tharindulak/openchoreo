# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The persisted record: what the receiver answered, and why the stage
failed on the runs it never reached.

Nothing here comes from the model. `classification` is read back from AE;
the issue number, url and `deduped` are stamped from the wire; `provider_facts`
is carried verbatim. `failure_reason` is the one field the model never
touches at all — set only by `failed()`, for a stage that threw before it
could even call `ae_create_issue`.
"""

from src.agent.handoff_provider import HandoffProvider
from src.models.rca_report import HandoffClassification, HandoffResult

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


def test_the_issue_facts_come_from_the_wire_not_the_model():
    outcome = {
        "called": True,
        "ref": 7,
        "link": "https://x/7",
        "existing": False,
        "facts": {"accepted": True, "attempt": 2},
    }

    record = HandoffResult.compose(outcome, PROVIDER)

    assert record.created_issue_number == 7
    assert record.created_issue_url == "https://x/7"
    assert record.deduped is False
    # Carried verbatim under the receiver's own names, never interpreted here.
    assert record.provider_facts == {"accepted": True, "attempt": 2}
    # Nothing failed, so there is nothing to say.
    assert record.failure_reason is None


def test_classification_is_read_back_not_computed():
    record = HandoffResult.compose({"classification": "mixed"}, PROVIDER)
    assert record.classification is HandoffClassification.MIXED


def test_an_empty_outcome_means_the_create_tool_was_never_reached():
    record = HandoffResult.compose({}, PROVIDER)

    assert record.created_issue_number is None
    assert record.created_issue_url is None
    assert record.deduped is False
    assert record.provider_facts == {}


def test_failed_records_code_level_and_names_the_failure():
    # A stage that threw must not read as a stage that decided. The
    # classification stays truthful so the console does not relabel a code-level
    # incident as nothing-to-do, and failure_reason says what actually happened
    # — the one thing a successful compose() never sets.
    record = HandoffResult.failed(RuntimeError("Skill 'x' not found"))
    assert record.classification is HandoffClassification.CODE_LEVEL
    assert "Skill 'x' not found" in record.failure_reason
    assert record.created_issue_number is None
    assert record.created_issue_url is None
    assert record.deduped is False
