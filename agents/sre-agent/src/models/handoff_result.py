# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class HandoffSummary(BaseModel):
    """The stage's structured output: what it did, and the links it found.

    Deliberately carries no decision. Whether code-level work exists follows
    from remediation's own statuses, which AE derives and answers, and whether
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
        description="Existing issues found related to this root cause",
    )


class HandoffResult(BaseModel):
    """The persisted record of the handoff — the agent's summary plus the facts.

    Only `rationale` and `related_issues` come from the model. `classification`
    is read back from what AE derived off remediation's statuses, never
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
        description="Existing issues found related to this root cause",
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
    def _classification_from(
        cls, outcome: dict[str, Any], provider: "HandoffProvider"
    ) -> HandoffClassification:
        """Read back the classification the RECEIVER derived.

        It is not derived here. What the remediation statuses mean is AE's
        contract, it is the same rule AE's own escalation reads, and two
        derivations of one fact are two things that can disagree. AE spells the
        values with hyphens; this enum spells them with underscores.

        An unanswered call — the stage threw, the model ended its turn without
        calling create, the receiver answered something unrecognised — records
        `code_level`. That is the safe direction and the one the read side needs:
        an absent value there defaults to `none`, which would relabel a real
        code-level incident as nothing-to-do on the report somebody triages.
        """
        answered = outcome.get(provider.answer_classification)
        if isinstance(answered, str):
            try:
                return HandoffClassification(answered.replace("-", "_"))
            except ValueError:
                pass
        return HandoffClassification.CODE_LEVEL

    @classmethod
    def compose(
        cls,
        summary: "HandoffSummary",
        outcome: dict[str, Any],
        provider: "HandoffProvider",
    ) -> "HandoffResult":
        """Assemble the persisted record.

        An empty outcome means the create tool was never reached — the stage
        errored, or the model ended its turn without calling it. AE's own
        escalation is what notices the absent issue; nothing here retries.
        """
        return cls(
            classification=cls._classification_from(outcome, provider),
            rationale=summary.rationale,
            related_issues=summary.related_issues,
            created_issue_number=outcome.get(provider.answer_issue_number),
            created_issue_url=outcome.get(provider.answer_issue_url),
            deduped=bool(outcome.get(provider.answer_already_filed)),
            provider_facts=dict(outcome.get("facts") or {}),
        )

    @classmethod
    def failed(cls, error: BaseException) -> HandoffResult:
        """The record for a stage that threw before it could file.

        Without this the report simply has no `handoff` key, which is the shape
        a legitimate "nothing was handed over" also has — so a crashed loader
        reads as a decision. The incident itself is safe either way: AE files
        the issue this stage owed, because its escalation keys off the absent
        issue number rather than anything said here.

        It records `code_level` for the same reason an unanswered create call
        does: the read side defaults an absent value to `none`, which would
        relabel a code-level incident as nothing-to-do on the very report
        somebody triages, and the classification is AE's to answer — a stage
        that never reached AE has no answer to record.
        """
        return cls(
            classification=HandoffClassification.CODE_LEVEL,
            rationale=f"The handoff stage failed before it could file: {error}",
        )
