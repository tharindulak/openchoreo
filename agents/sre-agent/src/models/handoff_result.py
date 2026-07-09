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


class HandoffResult(BaseModel):
    """Structured output from the handoff agent - AE coding-agent handoff decision"""

    classification: HandoffClassification = Field(
        ..., description="Whether the identified fix is config-level, code-level, mixed, or none"
    )
    rationale: str = Field(
        ..., description="Why this classification was chosen, referencing the root cause"
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
    dispatch_run_name: str | None = Field(
        default=None, description="Name of the AE coding-agent run dispatched against the issue"
    )
