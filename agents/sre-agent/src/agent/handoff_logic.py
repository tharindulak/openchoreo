# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic parts of the AE handoff decision.

The handoff LLM agent (`prompts/handoff_agent_prompt.j2`) keeps the genuinely
judgment-based work: deciding whether an ambiguous root cause needs a code
change, searching for related issues, judging their relevance, and writing
the issue body. Everything that is a mechanical fact about the data — the
config_level/none classification, the dedupe key, the SRE-Agent tag, and the
"never dispatch without a fresh issue" rule — is enforced here in code so it
can't be skipped by an off-prompt LLM response.
"""

import json
import logging
import re
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from src.agent.fingerprint import error_fingerprint
from src.agent.tool_registry import TOOLS
from src.helpers import AlertScope
from src.models.handoff_result import HandoffClassification, HandoffResult
from src.models.remediation_result import ActionStatus

logger = logging.getLogger(__name__)

# Applied to every issue the handoff stage creates, in addition to whatever
# labels the LLM chooses — lets a human (or a sweep job) list every
# SRE-agent-filed issue project-wide with `label:sre-agent`, independent of
# the per-component dedupe key used for idempotency.
SRE_AGENT_LABEL = "sre-agent"

# aep-api's tasks-github-native marker scheme (services/aep-api/internal/contracts/
# taskmeta: labels.go, block.go). Every issue this stage creates is already a
# code-level fix (classify_handoff_shortcut short-circuits config_level/none
# before the LLM ever runs), so the class is always "coding". Stamping these at
# creation — instead of relying solely on PromoteAndExecute's later promotion —
# means the issue is a well-formed Task immediately, before dispatch is ever
# attempted. `aep:execute` is deliberately excluded: it is the actual dispatch
# trigger and stays a distinct, later action (see OnLabeled in aep-api).
TASKMETA_LABELS = ["aep:task", "aep:coding", "aep:origin/incident"]


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
    project-prefixed (e.g. `testyello-service1`), but AE's design (specs/design/
    components/<name>) and the funnel's dispatch-time gate key on the UNPREFIXED
    name (`service1`). Passing the prefixed name makes the gate cancel the run
    with "component not in design at HEAD". aep-api's own EnsureComponent expects
    the unprefixed name too. The issue-fix skill already tells the LLM to strip
    this prefix by hand (SKILL.md) — doing it deterministically here removes that
    per-call dependency so a stray prefixed name can't silently kill dispatch.
    """
    if project and component.startswith(f"{project}-"):
        return component[len(project) + 1 :]
    return component


def _taskmeta_block(component: str) -> str:
    """Mirrors taskmeta.Block.Serialize() for a fresh incident-origin coding
    Task — the `<!-- aep:task/v1 ... -->` machine block aep-api's webhook
    handlers (OnOpenedOrEdited/OnLabeled) require to recognize an issue as a
    dispatchable Task at all. `component` MUST be the design (unprefixed) name —
    see design_component_name."""
    return f"<!-- aep:task/v1\ncomponent: {component}\norigin: incident\n-->\n\n"


def _ensure_taskmeta_block(body: str, component: str) -> str:
    """Guarantee the issue body carries a taskmeta block whose `component:` is the
    design (unprefixed) name, and return the corrected body.

    The handoff LLM sometimes writes its OWN block (copied from the related
    issues it reads via ae_search_related_issues, which carry the project-PREFIXED
    OpenChoreo name), so a "inject only when absent" guard silently defers to the
    wrong name — the funnel gate reads THIS block (not the dispatch call's param)
    and cancels with "component not in design at HEAD". So be authoritative: if a
    leading block exists, rewrite its component line to the design name (adding
    the line if the LLM's block omitted it); otherwise inject a fresh block.
    """
    if not body.lstrip().startswith("<!-- aep:task/v1"):
        return _taskmeta_block(component) + body

    end = body.find("-->")
    if end == -1:  # malformed/unterminated block — replace defensively
        return _taskmeta_block(component) + body
    head, tail = body[:end], body[end:]
    new_head, n = re.subn(
        r"(?m)^(\s*component:\s*).*$", r"\g<1>" + component, head, count=1
    )
    if n == 0:  # block had no component line — insert one after the opener
        new_head = head.replace(
            "<!-- aep:task/v1", f"<!-- aep:task/v1\ncomponent: {component}", 1
        )
    return new_head + tail


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

    Returns None when genuine judgment is required (leaving it to the LLM):
    remediation didn't run at all (actions have no `status`), or at least one
    action is still `suggested` — the LLM decides whether that's code_level
    or mixed, since `status` alone isn't a reliable enough signal for that
    call (per the prompt's own caveat).
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


class _HandoffCallState:
    def __init__(self) -> None:
        self.created_issue_number: int | None = None
        self.deduped = False


def wrap_ae_tools_for_handoff(
    tools: list[BaseTool],
    scope: AlertScope,
    report_context: dict[str, Any] | None = None,
) -> list[BaseTool]:
    """Wrap `ae_create_issue`/`ae_dispatch_coding_agent` so the dedupe key, the
    SRE-Agent tag, and the dispatch guard are structural guarantees rather than
    prompt instructions the LLM has to remember every time.

    report_context is the RCA report (model_dump) for this incident; when
    supplied its error signature is folded into the dedupe key so distinct root
    causes on the same component get distinct issues (see fingerprint.py).
    """
    if scope.component is None:
        return tools

    fingerprint = error_fingerprint(report_context)
    dedupe_key = dedupe_key_for(scope.component, fingerprint)
    # The design (unprefixed) name the taskmeta block and the dispatch call must
    # carry so aep-api's gate/EnsureComponent recognise the component.
    design_component = design_component_name(scope.component, scope.project)
    state = _HandoffCallState()
    wrapped: list[BaseTool] = []

    for tool in tools:
        if tool.name == TOOLS.AE_CREATE_ISSUE:
            wrapped.append(_wrap_create_issue(tool, dedupe_key, state, design_component))
        elif tool.name == TOOLS.AE_DISPATCH_CODING_AGENT:
            wrapped.append(_wrap_dispatch(tool, state, design_component))
        else:
            wrapped.append(tool)

    return wrapped


def _wrap_create_issue(
    tool: BaseTool, dedupe_key: str, state: _HandoffCallState, component: str
) -> BaseTool:
    async def _run(**kwargs: Any) -> str:
        kwargs["dedupeKey"] = dedupe_key
        labels = [str(v) for v in (kwargs.get("labels") or [])]
        for label in (SRE_AGENT_LABEL, *TASKMETA_LABELS):
            if label not in labels:
                labels.append(label)
        kwargs["labels"] = labels

        kwargs["body"] = _ensure_taskmeta_block(str(kwargs.get("body") or ""), component)

        raw = await tool.ainvoke(kwargs)
        try:
            result = _parse_tool_result(raw)
            state.deduped = bool(result.get("deduped"))
            state.created_issue_number = result.get("number")
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            logger.warning("Could not parse ae_create_issue result for dispatch guard: %r", raw)
        return raw

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def _wrap_dispatch(tool: BaseTool, state: _HandoffCallState, design_component: str) -> BaseTool:
    async def _run(**kwargs: Any) -> str:
        if state.created_issue_number is None or state.deduped:
            return json.dumps(
                {
                    "error": (
                        "Dispatch blocked: ae_dispatch_coding_agent can only be called after "
                        "ae_create_issue created a NEW (non-deduped) issue in this same run."
                    )
                }
            )
        # Force the design (unprefixed) component name regardless of what the LLM
        # passed. aep-api's PromoteAndExecute → EnsureComponent and the funnel
        # gate both key on this name; a prefixed name fails EnsureComponent and
        # cancels the run. This makes the normalisation structural instead of
        # relying on the LLM to strip the prefix per the skill instruction.
        kwargs["componentName"] = design_component
        return await tool.ainvoke(kwargs)

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )
