# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""What the issue-writing stage is allowed to see.

Two things are withheld and both are boundaries rather than instructions — the
model cannot weigh what it never receives. The observability recommendations are
advice about FUTURE analyses and are the main source of issues a coding agent
cannot act on; the ReleaseBinding patch on an already-handled action is an
invitation to express configuration as code and open a wrong pull request.
"""

from src.models.rca_report import handoff_view


def _report():
    return {
        "summary": "s",
        "result": {
            "type": "root_cause_identified",
            "recommendations": {
                "recommended_actions": [
                    {"description": "guard the input", "status": "suggested"},
                    {
                        "description": "raise the memory limit",
                        "status": "revised",
                        "change": {"jsonPointer": "/spec/x", "value": "2Gi"},
                    },
                ],
                "observability_recommendations": [{"description": "add a metric"}],
            },
        },
    }


def test_the_observability_advice_never_reaches_the_stage():
    view = handoff_view(_report())

    assert "observability_recommendations" not in view["result"]["recommendations"]


def test_a_config_patch_is_withheld_but_the_fact_it_happened_is_not():
    actions = handoff_view(_report())["result"]["recommendations"]["recommended_actions"]

    revised = actions[1]
    assert "change" not in revised
    # The description and status stay: without them the stage writes an issue
    # asking for a fix configuration already made.
    assert revised["status"] == "revised"
    assert revised["description"] == "raise the memory limit"


def test_a_pending_action_is_passed_through_whole():
    actions = handoff_view(_report())["result"]["recommendations"]["recommended_actions"]

    assert actions[0] == {"description": "guard the input", "status": "suggested"}


def test_the_stored_report_is_untouched():
    report = _report()

    handoff_view(report)

    # The console renders the observability advice and the config patches, so
    # trimming the stored report would delete something a human is meant to read.
    assert "observability_recommendations" in report["result"]["recommendations"]
    assert "change" in report["result"]["recommendations"]["recommended_actions"][1]


def test_a_report_without_recommendations_is_returned_as_is():
    assert handoff_view({"summary": "s"}) == {"summary": "s"}
    assert handoff_view({"result": {"type": "no_root_cause_identified"}}) == {
        "result": {"type": "no_root_cause_identified"}
    }
