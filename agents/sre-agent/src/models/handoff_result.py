# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.models.remediation_result import ActionStatus

# A status that needs nothing further from anybody. Anything else — including a
# status that is absent entirely — is pending work.
_SETTLED = frozenset({ActionStatus.REVISED, ActionStatus.APPLIED, ActionStatus.DISMISSED})


class HandoffClassification(StrEnum):
    """Whether the fix requires a config change, a code change, or both"""

    CONFIG_LEVEL = "config_level"
    CODE_LEVEL = "code_level"
    MIXED = "mixed"
    NONE = "none"

    @classmethod
    def derive(cls, recommended_actions: list[dict[str, Any]]) -> HandoffClassification:
        """Read the classification off remediation's own statuses.

        No model input reaches this. The remediation agent already decided
        code-versus-config — `revised` means it expressed the action as a
        ReleaseBinding change, `suggested` means it could not — and the handoff's
        old job of re-deciding that is exactly what failed on two live reports.

        An absent status is PENDING, not settled: remediation did not run, so
        there is no upstream determination to read, and an unfiled defect is
        dropped for good while an unnecessary issue is closed in minutes.
        """
        if not recommended_actions:
            return cls.NONE
        statuses = [a.get("status") for a in recommended_actions]
        pending = any(s not in _SETTLED for s in statuses)
        config_handled = ActionStatus.REVISED in statuses
        if not pending:
            return cls.CONFIG_LEVEL if config_handled else cls.NONE
        return cls.MIXED if config_handled else cls.CODE_LEVEL

    @property
    def needs_stage(self) -> bool:
        """Whether the issue-writing stage has anything to do.

        The gate, not a cost optimisation: a classification with no pending work
        cannot produce an issue, so running an LLM over it can only ever conclude
        that there was nothing to hand over.
        """
        return self in (HandoffClassification.CODE_LEVEL, HandoffClassification.MIXED)


class RelatedIssue(BaseModel):
    """An existing GitHub issue found to be related to this root cause"""

    number: int = Field(..., description="GitHub issue number")
    url: str = Field(..., description="GitHub issue URL")
    title: str = Field(..., description="GitHub issue title")


class HandoffSummary(BaseModel):
    """The stage's structured output: what it did, and the links it found.

    Deliberately carries no decision. Whether code-level work exists follows
    from remediation's own statuses (HandoffClassification.derive), and whether
    a fix is warranted is the coding agent's call, made with the repository and
    the spec in front of it. A model asked to restate either would be in a
    position to contradict the data it was handed — which is what happened.
    """

    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(
        ...,
        description=(
            "What was filed and what it hands over, referencing the root cause; or, when "
            "the create call answered deduped/suppressed/reopened, what that means for "
            "this incident"
        ),
    )
    related_issues: list[RelatedIssue] = Field(
        default_factory=list,
        description="Existing GitHub issues found related to this root cause",
    )


class HandoffResult(BaseModel):
    """The persisted record of the handoff — the agent's summary plus the facts.

    Only `rationale` and `related_issues` come from the model. `classification`
    is DERIVED from remediation's statuses (HandoffClassification.derive), never
    restated by the model. The issue number, url and `deduped` are stamped from
    what the receiver answered.

    Everything ELSE the receiver said lands in `provider_facts`, untyped and
    uninterpreted. That is deliberate: those fields belong to whatever system
    received the handoff, this agent cannot verify them, and giving them typed
    homes here meant the record could only ever describe one receiver.
    """

    classification: HandoffClassification = Field(
        ...,
        description=(
            "Whether the identified fix is config-level, code-level, mixed, or none. "
            "Derived, never restated by the model"
        ),
    )
    rationale: str = Field(
        ..., description="Why a code change is or is not required, referencing the root cause"
    )
    related_issues: list[RelatedIssue] = Field(
        default_factory=list,
        description="Existing GitHub issues found related to this root cause",
    )
    deduped: bool = Field(
        default=False,
        description=(
            "True when the create call folded onto an issue that already existed rather "
            "than filing a new one. Kept as a first-class field because the agent ACTS on "
            "it — a deduped handoff is not published downstream, since the run that "
            "created the issue already reported this incident"
        ),
    )
    created_issue_number: int | None = Field(
        default=None, description="Number of the GitHub issue created for the code-level fix"
    )
    created_issue_url: str | None = Field(
        default=None, description="URL of the GitHub issue created for the code-level fix"
    )
    provider_facts: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Whatever else the receiving platform answered, carried verbatim and never "
            "interpreted here — whether it handed the issue to a coding agent, why it did "
            "not, whether this incident had recurred, which attempt it is. Which keys "
            "appear is the receiver's business (see the handoff provider descriptor); a "
            "reader that understands that receiver reads them, and this agent does not "
            "paraphrase a fact it cannot check"
        ),
    )

    @classmethod
    def compose(
        cls,
        classification: "HandoffClassification",
        summary: "HandoffSummary",
        outcome: dict[str, Any],
        provider: "HandoffProvider",
    ) -> "HandoffResult":
        """Assemble the persisted record.

        ``classification`` is passed in rather than re-derived: the stage
        already derived it to decide whether to run, and two derivations of
        one fact are two things that can disagree.

        An empty outcome means the create tool was never reached — the stage
        errored, or the model ended its turn without calling it. The derived
        classification still tells the truth about whether code work exists,
        and AE's own escalation is what notices the absent issue.
        """
        return cls(
            classification=classification,
            rationale=summary.rationale,
            related_issues=summary.related_issues,
            created_issue_number=outcome.get(provider.answer_issue_number),
            created_issue_url=outcome.get(provider.answer_issue_url),
            deduped=bool(outcome.get(provider.answer_already_filed)),
            provider_facts=dict(outcome.get("facts") or {}),
        )

    @classmethod
    def without_handoff(
        cls,
        classification: HandoffClassification,
        recommended_actions: list[dict[str, Any]],
    ) -> HandoffResult:
        """The record for an incident whose stage never ran.

        It still says why, in the report, because "no issue was filed" and "the
        handoff never ran" look identical to a human triaging an alert.
        """
        if classification is HandoffClassification.CONFIG_LEVEL:
            rationale = (
                f"All {len(recommended_actions)} recommended action(s) were already translated "
                "into OpenChoreo ReleaseBinding configuration changes by the remediation agent "
                "— no source code change is required."
            )
        else:
            rationale = "No recommended action requires further work: " + (
                "the remediation agent produced no recommended actions."
                if not recommended_actions
                else "the remaining actions were already applied or dismissed."
            )
        return cls(classification=classification, rationale=rationale)
