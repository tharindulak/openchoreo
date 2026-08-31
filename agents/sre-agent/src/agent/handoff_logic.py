# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic parts of the AE handoff decision.

The handoff LLM agent (`prompts/handoff_agent_prompt.j2`) keeps the genuinely
judgment-based work: deciding whether a root cause needs a code change at all,
searching for related issues, judging their relevance, and writing the issue
body. Everything that is a mechanical fact about the data — the CLASSIFICATION,
the dedupe key, the SRE-Agent tag, the design component name, and whether the
issue is handed to the coding agent at all — is enforced here in code so it
can't be skipped by an off-prompt LLM response.

Classification is derived, never asked for. code-level / config-level / mixed /
none follows from remediation's `status` field plus one boolean the model does
own (`needs_code_change`), so the model is never in a position to contradict the
data it was handed. See derive_classification.

There is no dispatch step to guard any more. AE adopts an issue as it creates
it (`ae_create_issue`'s `adopt`, default true), so filing and handing over are
one call that cannot come apart — and the operator's `AE_AUTO_DISPATCH` switch
is applied here, on that one call, rather than trusted to the prompt.
"""

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from src.agent.fingerprint import error_fingerprint
from src.agent.tool_registry import TOOLS
from src.helpers import AlertScope
from src.models.handoff_result import (
    HandoffClassification,
    HandoffJudgment,
    HandoffResult,
    RuledOutReason,
)
from src.models.remediation_result import ActionStatus

logger = logging.getLogger(__name__)

# Applied to every issue the handoff stage creates, in addition to whatever
# labels the LLM chooses — lets a human (or a sweep job) list every
# SRE-agent-filed issue project-wide with `label:sre-agent`, independent of
# the per-component dedupe key used for idempotency.
SRE_AGENT_LABEL = "sre-agent"


def dedupe_key_for(component: str, fingerprint: str | None = None) -> str:
    """Per-incident dedupe key.

    Without a fingerprint the key is component-scoped (``sre-rca/<component>``),
    so every incident on the component folds onto one open issue. With a
    fingerprint of the triggering error signature the key becomes
    ``sre-rca/<component>/<fingerprint>`` — identical recurrences still dedupe,
    but a genuinely different root cause on the same component opens its own
    issue instead of being suppressed behind the first one.
    """
    base = f"sre-rca/{component}"
    return f"{base}/{fingerprint}" if fingerprint else base


def design_component_name(component: str, project: str) -> str:
    """Strip the `<project>-` prefix off an alert-scope component name to get the
    name AE's design model uses.

    The observability alert scope carries the OpenChoreo component name, which is
    project-prefixed (e.g. `testyello-service1`), but AE's design lives under
    specs/design/components/<name> with the UNPREFIXED name (`service1`), and
    that is what aep-api resolves `componentName` against before it adopts the
    issue. A prefixed name therefore FAILS the create call outright.

    That refusal is deliberate on AE's side and this function is why it should
    never fire: the LLM kept copying the prefixed name out of the related issues
    it had just read, so the normalisation is done here, once, in code.
    """
    if project and component.startswith(f"{project}-"):
        return component[len(project) + 1 :]
    return component


def _parse_tool_result(raw: Any) -> dict[str, Any]:
    """Normalizes an MCP tool call's return value into a dict.

    `BaseTool.ainvoke` doesn't return one consistent shape: it can be the
    provider's raw JSON string, an already-decoded dict, or — what the real
    MCP adapter actually returns for a text-content tool result — a list of
    content blocks (`[{"type": "text", "text": "<json>", "id": "..."}]`,
    mirroring Anthropic's own content-block schema). Only exercising this
    against a plain-string fake in tests let a live run hit the list shape
    unhandled, which silently broke the dispatch guard (see git history).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block.get("text", ""))
        raise ValueError(f"no text content block in tool result: {raw!r}")
    raise TypeError(f"unexpected tool result type {type(raw)!r}: {raw!r}")


def classify_handoff_shortcut(
    recommended_actions: list[dict[str, Any]],
) -> HandoffClassification | None:
    """Decide classification directly from remediation's status field when the
    data makes it unambiguous, skipping the handoff LLM call entirely.

    Returns None when genuine judgment is required, which is exactly one
    question: does the remaining work need a code change? That happens when at
    least one action is still `suggested`, or when remediation didn't run at all
    (the actions then carry no `status`, so nothing is derivable and the model
    judges from the root cause alone).

    Note what this does NOT defer: once the model answers that question,
    code-level vs mixed follows from the statuses. See derive_classification.
    """
    if not recommended_actions:
        return HandoffClassification.NONE

    statuses = {a.get("status") for a in recommended_actions}

    if None in statuses:
        return None

    if ActionStatus.SUGGESTED in statuses:
        return None

    if ActionStatus.REVISED in statuses:
        return HandoffClassification.CONFIG_LEVEL

    # Only APPLIED/DISMISSED actions remain — nothing pending either way.
    return HandoffClassification.NONE


def build_shortcut_result(
    classification: HandoffClassification, recommended_actions: list[dict[str, Any]]
) -> HandoffResult:
    if classification is HandoffClassification.NONE:
        rationale = (
            "No recommended action requires further work: "
            + (
                "the remediation agent produced no recommended actions."
                if not recommended_actions
                else "the remaining actions were already applied or dismissed."
            )
        )
    else:
        rationale = (
            f"All {len(recommended_actions)} recommended action(s) were already translated "
            "into OpenChoreo ReleaseBinding changes by the remediation agent — no source "
            "code change is required."
        )
    return HandoffResult(classification=classification, rationale=rationale)


def handoff_input(report_data: dict[str, Any]) -> dict[str, Any]:
    """The report as the handoff stage should see it. Two things are withheld, and
    both are boundaries rather than judgments — the model cannot weigh what it
    never sees.

    **The observability recommendations, entirely.** `recommended_actions` is how
    this incident gets fixed; `observability_recommendations` is advice for making
    FUTURE analyses easier ("add a metric", "raise the log level"), and it is the
    main source of issues a coding agent cannot act on. This does NOT narrow what
    a code-level fix can be: a logging gap that is part of THIS incident's root
    cause arrives as a recommended_action and is filed like any other.

    **The `change` patch on every already-handled (`revised`) action.** The issue
    this stage writes becomes a coding agent's prompt, and that agent can only
    edit the repository — so a concrete ReleaseBinding patch in front of it is an
    invitation to express config as code and open a wrong pull request. The
    action's description and status stay, because the model does need to know that
    part of the incident was already fixed by configuration; otherwise it writes
    an issue asking for that fix again, in code.

    Returns a copied view, deep only where it edits. The stored report keeps every
    field: the console renders the observability recommendations and the config
    patches, so trimming report_data itself would delete something a human is
    meant to read.
    """
    result = report_data.get("result")
    if not isinstance(result, dict):
        return report_data
    recommendations = result.get("recommendations")
    if not isinstance(recommendations, dict):
        return report_data

    trimmed: dict[str, Any] = {
        k: v for k, v in recommendations.items() if k != "observability_recommendations"
    }
    actions = trimmed.get("recommended_actions")
    if isinstance(actions, list):
        trimmed["recommended_actions"] = [
            {k: v for k, v in action.items() if k != "change"}
            if isinstance(action, dict) and action.get("status") == ActionStatus.REVISED
            else action
            for action in actions
        ]
    return {
        **report_data,
        "result": {**result, "recommendations": trimmed},
    }


def unfiled_despite_judgment(
    judgment: HandoffJudgment, outcome: dict[str, Any] | None
) -> list[str]:
    """Catch the opposite failure to a bad decline: the agent concluded a code
    change IS needed and then filed nothing.

    Both failures end identically — no issue, and nothing retries a handoff — but
    only one of them looks like a decision. This one looks like agreement, which
    is what makes it easy to miss: the report says `code_level`, the console
    shows a classification, and there is no issue behind any of it.

    The signal is `called`, not the issue number. A create that was attempted and
    answered unreadably is a filing that probably happened; treating it as "never
    filed" would ask the model to file a second time. The dedupe key would fold
    the duplicate onto the first issue, but the run would still be reasoning from
    a false premise.

    One legitimate case produces the same shape and is deliberately NOT caught
    here: a clearly matching OPEN issue is already the handoff for this problem,
    so the skill says leave the judgment true, name that issue and file nothing.
    The correction below asks for exactly that, which lets the model say so
    rather than being forced into a duplicate.
    """
    if not judgment.needs_code_change:
        return []
    if outcome and outcome.get("called"):
        return []
    return [
        "you concluded that a code change IS required, but ae_create_issue was never "
        "called, so nothing was filed and nothing will work this incident"
    ]


def unfiled_correction_prompt() -> str:
    """The message sent when a judgment of "code change needed" filed nothing.

    Like the decline correction it names the gap rather than dictating the
    outcome — there IS a case where filing nothing is right, and the prompt has
    to leave room for it or it would manufacture duplicate issues.
    """
    return (
        "You decided this root cause needs a code change, but you did not call "
        "ae_create_issue, so nothing was filed and no coding agent will ever see this "
        "incident. Deciding is not the deliverable; the issue is.\n\n"
        "File it now. The one exception is a clearly matching OPEN issue that is already "
        "the handoff for this problem — if that is what you found, name it in "
        "related_issues and say in your rationale that it covers this. A CLOSED issue is "
        "not that exception: it means an earlier fix did not hold, and this still needs "
        "filing."
    )


def correction_prompt(failures: list[str]) -> str:
    """The one message sent back when a decline does not hold up.

    It names the specific failures rather than repeating the rules, and it does
    NOT tell the model what to conclude — an instruction to "file an issue" would
    buy compliance rather than judgment, and the point is that the second look is
    a real one. It only removes the escape that was actually used: as-designed.
    """
    bullets = "\n".join(f"  - {f}" for f in failures)
    return (
        "Your decision not to file does not hold up against the actions you were given:\n"
        f"{bullets}\n\n"
        "Look again, action by action. An action that names something in the codebase to "
        "change — logic, error handling, a timeout, a retry, a hardcoded value, an "
        "artificial delay — needs a code change, and that stays true when the behaviour "
        "is DELIBERATE: making it configurable or adding backoff hardens it without "
        "contradicting the spec. The platform's own implementation issues record what was "
        "built and what must be preserved; they never rule a change out.\n\n"
        "If a code change is warranted, file the issue now and say in it which behaviour "
        "must be preserved. If you still rule every action out, map each one to "
        "config_handled or pure_advice with a justification specific to that action."
    )


def unjustified_rulings(
    judgment: HandoffJudgment, recommended_actions: list[dict[str, Any]]
) -> list[str]:
    """Check a DECLINE against the data it was made over, and name what does not
    hold. An empty list means the decline is justified.

    Why this exists. `needs_code_change` is the one input the model owns, and it
    is owned for a good reason — no status field can express "this recommendation
    is a nicety, not a defect". But owning a judgment is not the same as being
    unaccountable for it, and the failure this catches was real: an RCA whose
    remediation asked to "remove the artificial delay in service2" was declined
    wholesale because the delay was deliberate and the platform's own
    implementation issues described it. One sentence about the incident silently
    dropped an action that named source code to change.

    So the model still decides, but it decides PER ACTION and shows its working,
    and two of the things it can claim are checkable without a model:

      - Every remaining action must be accounted for. An action nobody mentioned
        is an action nobody ruled out.
      - `config_handled` is a claim about remediation's own output, so it is only
        true of a `revised` action. Claiming it for a `suggested` one asserts a
        ReleaseBinding change that does not exist.

    `pure_advice` is deliberately NOT checked. It is the subjective half, and
    the whole reason a model is asked at all — but it now has to be asserted
    against a specific action, in writing, which is a far harder thing to do
    accidentally than agreeing that a system works as designed.

    Returns human-readable failures, in action order, for the correction prompt
    and for the report.
    """
    if judgment.needs_code_change:
        return []

    by_index = {r.index: r for r in judgment.ruled_out}
    failures: list[str] = []
    for i, action in enumerate(recommended_actions):
        description = str(action.get("description") or "").strip()
        summary = (description[:160] + "…") if len(description) > 160 else description
        ruling = by_index.get(i)
        if ruling is None:
            failures.append(
                f"action[{i}] was not ruled out at all, so nothing justifies dropping it: {summary!r}"
            )
            continue
        if (
            ruling.reason == RuledOutReason.CONFIG_HANDLED
            and action.get("status") != ActionStatus.REVISED
        ):
            failures.append(
                f"action[{i}] was called {RuledOutReason.CONFIG_HANDLED} but its status is "
                f"{action.get('status') or 'absent'}, not {ActionStatus.REVISED} — remediation "
                f"produced no configuration change for it: {summary!r}"
            )
    return failures


def derive_classification(
    needs_code_change: bool, recommended_actions: list[dict[str, Any]]
) -> HandoffClassification:
    """The classification, computed rather than claimed.

    Three rules, and every input is already on the table:

    - The model said no code change is needed → `none`. This is the one input it
      owns, because no status field can express "this recommendation is a vague
      nicety, not a defect".
    - Code needed, and some action was already translated into a ReleaseBinding
      change (`revised`) → `mixed`: part of this incident was fixed by config and
      part needs code.
    - Code needed and nothing was config-handled → `code_level`. Statusless
      actions land here too, which is correct: remediation did not run, so
      nothing was config-handled, and the model judged the root cause directly.
    """
    if not needs_code_change:
        return HandoffClassification.NONE
    if any(a.get("status") == ActionStatus.REVISED for a in recommended_actions):
        return HandoffClassification.MIXED
    return HandoffClassification.CODE_LEVEL


def compose_handoff_result(
    judgment: HandoffJudgment,
    recommended_actions: list[dict[str, Any]],
    outcome: dict[str, Any],
) -> HandoffResult:
    """Assemble the persisted record: the model's judgment, the derived
    classification, and the facts `ae_create_issue` answered.

    The split is the point. `rationale` and `related_issues` are the model's and
    are copied verbatim. `classification` is derived. The issue number, url,
    dedupe, recurrence and adoption state are stamped from the wire, because the
    console's Alerts list serves them as-is — a model restating one loosely would
    show a human the wrong state while they triage an incident. `reopened` and
    `recurrence` matter most of all here: they say a fix AE already merged for
    this incident did not work, which is the one fact a reader must not get from
    a paraphrase.

    An empty outcome means `ae_create_issue` was never called: the model declined
    to file, or a matching open issue made filing unnecessary. Either way there is
    nothing to stamp, and the derived classification still tells the truth about
    whether code work exists.
    """
    return HandoffResult(
        classification=derive_classification(judgment.needs_code_change, recommended_actions),
        rationale=judgment.rationale,
        related_issues=judgment.related_issues,
        created_issue_number=outcome.get("number"),
        created_issue_url=outcome.get("url"),
        ruled_out=judgment.ruled_out,
        deduped=bool(outcome.get("deduped")),
        reopened=bool(outcome.get("reopened")),
        recurrence=int(outcome.get("recurrence") or 0),
        adopted=bool(outcome.get("adopted")),
        adoption_error=outcome.get("adoption_error"),
    )


def wrap_ae_tools_for_handoff(
    tools: list[BaseTool],
    scope: AlertScope,
    report_context: dict[str, Any] | None = None,
    auto_dispatch: bool = True,
    outcome: dict[str, Any] | None = None,
) -> list[BaseTool]:
    """Wrap `ae_create_issue` so the dedupe key, the SRE-Agent tag, the design
    component name, and the auto-dispatch decision are structural guarantees
    rather than prompt instructions the LLM has to remember every time.

    Every one of them is a mechanical fact about THIS incident, not a judgment:
    the LLM's job is the issue's title and body, and each of these was, at some
    point, something it got wrong.

    report_context is the RCA report (model_dump) for this incident; when
    supplied its error signature is folded into the dedupe key so distinct root
    causes on the same component get distinct issues (see fingerprint.py).

    auto_dispatch is the operator's `AE_AUTO_DISPATCH`. It is applied HERE, on
    the create call, because that call is the dispatch: false files a ledger
    entry that waits for a human instead.

    outcome, when given, is filled with what `ae_create_issue` ANSWERED — the
    issue number and url, and whether it deduped or was adopted. The report's
    facts are then taken from there rather than from the LLM's structured
    response (see compose_handoff_result): a field the model has to restate is a
    field it can restate wrongly, and this one drives what the console shows.
    """
    if scope.component is None:
        return tools

    fingerprint = error_fingerprint(report_context)
    dedupe_key = dedupe_key_for(scope.component, fingerprint)
    # The design (unprefixed) name aep-api resolves `componentName` against
    # before it will adopt the issue.
    design_component = design_component_name(scope.component, scope.project)

    wrapped: list[BaseTool] = []
    for tool in tools:
        if tool.name == TOOLS.AE_CREATE_ISSUE:
            wrapped.append(
                _wrap_create_issue(tool, dedupe_key, design_component, auto_dispatch, outcome)
            )
        elif tool.name == TOOLS.AE_SEARCH_RELATED_ISSUES:
            wrapped.append(_wrap_search_related_issues(tool))
        else:
            wrapped.append(tool)
    return wrapped


# The label AE puts on its own planned work. An issue carrying it was written by
# the platform to describe what to BUILD, so it is a record of the spec — which
# is exactly what makes it dangerous to read as evidence that nothing is wrong.
PLATFORM_WORK_LABEL = "aep"

# Attached to every such issue in the search result. The skill says the same
# thing in prose; this says it at the point of use, in the document the model is
# reading, because prose in a skill did not survive contact with an issue titled
# "Implement service2 slow backend" while the model was deciding whether a slow
# service2 was a defect.
PLATFORM_ISSUE_NOTE = (
    "PLATFORM IMPLEMENTATION RECORD — this issue is AE's own plan for what to BUILD. "
    "It tells you which behaviour must be PRESERVED. It is not a defect report and it "
    "is never grounds for ruling out a code change: behaviour can be deliberate and "
    "still be worth hardening."
)


def annotate_platform_issues(issues: list[Any]) -> list[Any]:
    """Stamp AE's own implementation issues with what they are.

    Pure and list-shaped so it can be tested without a tool: the wrapper below is
    only plumbing. Anything that is not a dict, or carries no labels, is passed
    through untouched — a search result is the other side's shape, and mangling
    an unexpected record would cost more than the annotation gains.
    """
    out: list[Any] = []
    for issue in issues:
        if not isinstance(issue, dict):
            out.append(issue)
            continue
        labels = issue.get("Labels") or issue.get("labels") or []
        if isinstance(labels, list) and any(
            isinstance(label, str) and label.strip().lower() == PLATFORM_WORK_LABEL
            for label in labels
        ):
            issue = {**issue, "PlatformRecord": True, "ReadAs": PLATFORM_ISSUE_NOTE}
        out.append(issue)
    return out


def _wrap_search_related_issues(tool: BaseTool) -> BaseTool:
    """Wrap `ae_search_related_issues` so AE's own planned-work issues arrive
    labelled as the spec record they are.

    This is the same discipline `_wrap_create_issue` follows: what the model must
    not get wrong is guaranteed in CODE rather than asked for in a prompt. An
    unreadable result is returned untouched — the annotation is a help, and
    losing the search entirely to a parse error would be a far worse trade.
    """

    async def _run(**kwargs: Any) -> str:
        raw = await tool.ainvoke(kwargs)
        try:
            parsed = _parse_tool_result(raw)
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            # A search answers with an ARRAY, so a bare list of issue records is
            # a shape _parse_tool_result rejects (it looks for content blocks).
            # That is this tool's normal answer, not an error.
            if isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
                return json.dumps(annotate_platform_issues(raw))
            return raw
        if not isinstance(parsed, list):
            return raw
        return json.dumps(annotate_platform_issues(parsed))

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def _wrap_create_issue(
    tool: BaseTool,
    dedupe_key: str,
    component: str,
    auto_dispatch: bool,
    outcome: dict[str, Any] | None,
) -> BaseTool:
    async def _run(**kwargs: Any) -> str:
        kwargs["dedupeKey"] = dedupe_key
        # Forced, not defaulted: the LLM copies the project-PREFIXED name out of
        # the related issues it just read, and aep-api refuses that name.
        kwargs["componentName"] = component
        kwargs["adopt"] = auto_dispatch
        labels = [str(v) for v in (kwargs.get("labels") or [])]
        if SRE_AGENT_LABEL not in labels:
            labels.append(SRE_AGENT_LABEL)
        kwargs["labels"] = labels

        if outcome is not None:
            # Stamped BEFORE the call, and separately from the answer, because
            # the two say different things: `called` means the agent actually
            # tried to file, while the fields below mean AE answered. An attempt
            # whose answer could not be parsed is still an attempt, and must not
            # be mistaken for an agent that never filed at all.
            outcome["called"] = True

        raw = await tool.ainvoke(kwargs)
        if outcome is not None:
            try:
                result = _parse_tool_result(raw)
                outcome.update(
                    {
                        "number": result.get("number"),
                        "url": result.get("url"),
                        "deduped": bool(result.get("deduped")),
                        "reopened": bool(result.get("reopened")),
                        "recurrence": int(result.get("recurrence") or 0),
                        "adopted": bool(result.get("adopted")),
                        "adoption_error": result.get("adoptionError") or None,
                    }
                )
            except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
                # An unreadable result is worth a warning, not a failure: the
                # issue may well exist, and the LLM still sees the raw answer.
                logger.warning("Could not parse ae_create_issue result: %r", raw)
        return raw

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )
