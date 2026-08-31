# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from enum import StrEnum

from pydantic import BaseModel, Field


class HandoffClassification(StrEnum):
    """Whether the fix requires a config change, a code change, or both"""

    CONFIG_LEVEL = "config_level"
    CODE_LEVEL = "code_level"
    MIXED = "mixed"
    NONE = "none"


class RelatedIssue(BaseModel):
    """An existing GitHub issue found to be related to this root cause"""

    number: int = Field(..., description="GitHub issue number")
    url: str = Field(..., description="GitHub issue URL")
    title: str = Field(..., description="GitHub issue title")


class RuledOutReason(StrEnum):
    """The ONLY two grounds on which a recommended action may be ruled out.

    The list is closed on purpose. The handoff exists because an unfiled defect
    is dropped for good — nothing retries a handoff — so `needs_code_change =
    false` is the finding that has to be justified, and it is justified one
    ACTION AT A TIME rather than by a single sentence about the incident.

    What is deliberately absent is the reason that caused this to be added:
    "the system is behaving as designed". A deliberate delay that trips a short
    timeout IS intended, and making it configurable or adding backoff hardens it
    without contradicting the intent. As-designed is a fact about the spec, never
    a ground for ruling out a change.
    """

    CONFIG_HANDLED = "config_handled"
    """Already actionable as configuration — remediation revised it into a
    ReleaseBinding change. Machine-checkable: only true of a `revised` action."""

    PURE_ADVICE = "pure_advice"
    """No fault behind it — "consider monitoring this", "review capacity". The
    one genuinely subjective call, and the reason the model owns this decision at
    all: no status field can express "this is a nicety, not a defect"."""


class RuledOutAction(BaseModel):
    """One recommended action the model declined to file for, and why."""

    index: int = Field(
        ...,
        description=(
            "0-based position of the action in `recommendations.recommended_actions`, "
            "as it was given to you"
        ),
    )
    reason: RuledOutReason = Field(..., description="Which of the two permitted grounds applies")
    justification: str = Field(
        ...,
        description=(
            "One sentence naming what in THIS action makes that ground apply. "
            "'The system works as designed' is not a justification"
        ),
    )


class HandoffJudgment(BaseModel):
    """The handoff agent's structured output — its JUDGMENT, and nothing else.

    Deliberately carries no classification. Whether a fix is code-level, mixed or
    none follows mechanically from remediation's `status` field, so asking the
    model to restate it invites it to disagree with the data it was handed. It
    answers the one question the data cannot: does this root cause need a source
    code change at all? `HandoffResult` is composed from this plus what AE
    answered (see handoff_logic.compose_handoff_result).
    """

    needs_code_change: bool = Field(
        ...,
        description=(
            "True if resolving this root cause requires a source code change. False when "
            "the remaining recommendations do not warrant one — a vague observability "
            "nicety, or advice that is not a real defect. When false, file no issue"
        ),
    )
    rationale: str = Field(
        ...,
        description="Why a code change is or is not required, referencing the root cause",
    )
    related_issues: list[RelatedIssue] = Field(
        default_factory=list,
        description="Existing GitHub issues found related to this root cause",
    )
    ruled_out: list[RuledOutAction] = Field(
        default_factory=list,
        description=(
            "REQUIRED when needs_code_change is false: one entry for EVERY remaining "
            "recommended action, saying which of the two permitted grounds rules it out. "
            "An action you cannot map to one of them is an action that needs a code "
            "change. Ignored when needs_code_change is true"
        ),
    )


class HandoffResult(BaseModel):
    """The persisted record of the handoff — the agent's judgment plus the facts.

    Only `rationale` and `related_issues` come from the model. `classification` is
    DERIVED from the judgment and remediation's statuses, and the issue/adoption
    fields are stamped from what `ae_create_issue` answered, because the console
    reads them.
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
    ruled_out: list[RuledOutAction] = Field(
        default_factory=list,
        description=(
            "When no code change was needed: the per-action grounds for that, one entry "
            "per remaining recommended action. Kept on the record because a handoff that "
            "files nothing is otherwise indistinguishable from one that never ran"
        ),
    )
    deduped: bool = Field(
        default=False,
        description=(
            "True if ae_create_issue deduped onto an already-open issue this run "
            "rather than creating a new one"
        ),
    )
    reopened: bool = Field(
        default=False,
        description=(
            "True when this incident RECURRED: the dedupe key matched an issue AE had "
            "already closed as fixed, so AE reopened it with the new evidence and handed "
            "it back to the coding agent instead of filing a duplicate. The opposite of "
            "`deduped` in what it implies — deduped means nothing was done, reopened "
            "means a merged fix did not work"
        ),
    )
    recurrence: int = Field(
        default=0,
        description=(
            "Which attempt this is: 1 for a first filing, 2 for the first recurrence, and "
            "so on. 0 when AE could not establish it. Stamped from ae_create_issue's "
            "answer so a human triaging sees 'attempt 3' rather than a bare 'issue filed'"
        ),
    )
    created_issue_number: int | None = Field(
        default=None, description="Number of the GitHub issue created for the code-level fix"
    )
    created_issue_url: str | None = Field(
        default=None, description="URL of the GitHub issue created for the code-level fix"
    )
    adopted: bool = Field(
        default=False,
        description=(
            "True when AE adopted the created issue — it is in a version's milestone as "
            "agent work and a coding run has it. Stamped from ae_create_issue's answer, "
            "not restated by the model"
        ),
    )
    adoption_error: str | None = Field(
        default=None,
        description=(
            "Why AE did not adopt the issue, when it did not. The issue still exists as a "
            "ledger entry and a human can hand it over later"
        ),
    )
