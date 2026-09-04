# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The stage's own log line must not read fields off HandoffResult that the
model no longer carries.

The failure this guards is deceptive rather than loud: an AttributeError here is
raised after the handoff has been written onto the report, so the stage's
except-Exception reports "Handoff agent failed, saving RCA report without it"
about a handoff that actually succeeded.
"""

from src.agent.agent import _handoff_log_fields
from src.models.handoff_result import HandoffClassification, HandoffResult


def test_log_fields_come_from_the_result_and_its_carried_facts():
    result = HandoffResult(
        classification=HandoffClassification.CODE_LEVEL,
        rationale="unbounded input exhausts the raised limit",
        created_issue_url="https://github.com/o/r/issues/7",
        provider_facts={"adopted": True, "recurrence": 2},
    )

    classification, url, facts = _handoff_log_fields(result)

    assert classification == HandoffClassification.CODE_LEVEL
    assert url == "https://github.com/o/r/issues/7"
    # Carried verbatim: which keys exist is the receiver's business, so the log
    # must not name them one by one.
    assert facts == {"adopted": True, "recurrence": 2}


def test_log_fields_survive_a_receiver_that_answered_nothing():
    result = HandoffResult(
        classification=HandoffClassification.NONE,
        rationale="every remaining action was already applied",
    )

    classification, url, facts = _handoff_log_fields(result)

    assert classification == HandoffClassification.NONE
    assert url is None
    assert facts == {}
