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
    deduped: bool = Field(
        default=False,
        description=(
            "True if ae_create_issue deduped onto an already-open issue this run "
            "rather than creating a new one"
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
