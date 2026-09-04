# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Classification is read off the remediation agent's own statuses.

The remediation agent already decided code-versus-config: `revised` means it
expressed the action as a ReleaseBinding change, `suggested` means it could not.
Nothing here re-decides that, and no model input reaches it — which is the whole
point, because the model's veto over that upstream fact is what failed twice.
"""

from src.models.handoff_result import HandoffClassification, HandoffResult
from src.models.remediation_result import ActionStatus


def _actions(*statuses):
    return [
        {"description": f"action {i}", **({} if s is None else {"status": s})}
        for i, s in enumerate(statuses)
    ]


def test_no_actions_is_nothing_to_hand_over():
    assert HandoffClassification.derive([]) is HandoffClassification.NONE


def test_everything_settled_without_config_is_nothing_to_hand_over():
    actions = _actions(ActionStatus.APPLIED, ActionStatus.DISMISSED)
    assert HandoffClassification.derive(actions) is HandoffClassification.NONE


def test_everything_handled_by_configuration_is_config_level():
    actions = _actions(ActionStatus.REVISED, ActionStatus.APPLIED)
    assert HandoffClassification.derive(actions) is HandoffClassification.CONFIG_LEVEL


def test_a_pending_action_beside_a_config_change_is_mixed():
    actions = _actions(ActionStatus.SUGGESTED, ActionStatus.REVISED)
    assert HandoffClassification.derive(actions) is HandoffClassification.MIXED


def test_a_pending_action_alone_is_code_level():
    actions = _actions(ActionStatus.SUGGESTED)
    assert HandoffClassification.derive(actions) is HandoffClassification.CODE_LEVEL


def test_an_absent_status_counts_as_pending():
    """Remediation did not run, so nothing was triaged and no upstream
    determination exists. The safe reading is that work remains — and the AE
    backstop is blind to this case (it drops a status it does not recognise), so
    this predicate is the only thing that files it."""
    assert HandoffClassification.derive(_actions(None)) is HandoffClassification.CODE_LEVEL
    assert (
        HandoffClassification.derive(_actions(None, ActionStatus.REVISED))
        is HandoffClassification.MIXED
    )


def test_only_code_level_and_mixed_run_the_stage():
    assert HandoffClassification.CODE_LEVEL.needs_stage is True
    assert HandoffClassification.MIXED.needs_stage is True
    assert HandoffClassification.CONFIG_LEVEL.needs_stage is False
    assert HandoffClassification.NONE.needs_stage is False


def test_the_skipped_result_says_which_kind_of_nothing_happened():
    config = HandoffResult.without_handoff(
        HandoffClassification.CONFIG_LEVEL, _actions(ActionStatus.REVISED)
    )
    assert config.classification is HandoffClassification.CONFIG_LEVEL
    assert "configuration" in config.rationale
    assert config.created_issue_number is None

    empty = HandoffResult.without_handoff(HandoffClassification.NONE, [])
    assert "no recommended actions" in empty.rationale

    settled = HandoffResult.without_handoff(
        HandoffClassification.NONE, _actions(ActionStatus.APPLIED)
    )
    assert "applied or dismissed" in settled.rationale
