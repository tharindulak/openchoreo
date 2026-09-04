# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The stage's structured output carries what it DID, not what it decided.

`needs_code_change` is gone because it was a veto over a determination the
remediation agent had already made, and the per-action `ruled_out` machinery
existed only to police that veto. Nothing on this side rules an action out any
more: the coding agent has the repository and closes as not planned when no
change is warranted (ADR-0023).
"""

import pytest
from pydantic import ValidationError

from src.models.handoff_result import HandoffSummary, RelatedIssue


def test_the_summary_is_a_rationale_and_the_links_it_found():
    summary = HandoffSummary(
        rationale="filed; the unbounded input still needs a guard",
        related_issues=[
            RelatedIssue(number=12, url="https://github.com/o/r/issues/12", title="same handler")
        ],
    )

    assert summary.rationale.startswith("filed")
    assert summary.related_issues[0].number == 12


def test_related_issues_are_optional_because_a_search_may_find_nothing():
    assert HandoffSummary(rationale="filed").related_issues == []


def test_the_removed_decision_fields_are_gone_for_good():
    with pytest.raises(ValidationError):
        HandoffSummary(rationale="x", needs_code_change=False)


def test_the_ruled_out_vocabulary_no_longer_exists():
    import src.models.handoff_result as m

    for gone in ("HandoffJudgment", "RuledOutReason", "RuledOutAction"):
        assert not hasattr(m, gone), f"{gone} should have been deleted"
