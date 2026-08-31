# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Publisher for RCA reports to the AE console (aep-api).

The RCA agent stores every report in its own backend (sqlite/postgres via
``report_backend``). That copy powers the agent's own REST/chat API. It does
NOT reach the AE console: the console's Alerts bell + list (labs-agentic-engineer
issues #154/#155) read from aep-api's ``rca_agent_reports`` table, which is
populated only through the write endpoint added in PR #161:

    POST {ae_api_url}/api/v1/rca-agent/reports        (create-rca-agent-report)

This module is the missing producer for that endpoint. On each completed
analysis (gated by ``settings.ae_publish_reports``) ``run_analysis`` calls
:func:`publish_rca_report`, which maps the in-memory report dict onto aep-api's
``CreateRcaAgentReportRequest`` and POSTs it with the same OAuth2
client-credentials token the handoff already uses against aep-api. The
endpoint binds the org from that token (no org in the body). See
RCA-REPORT-PUBLISHING.md for the end-to-end wiring.
"""

import logging
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_REPORTS_PATH = "/api/v1/rca-agent/reports"

# aep-api's contract spells classifications with hyphens
# (code-level | config-level | mixed | none); the agent's HandoffClassification
# StrEnum uses underscores. Normalize on the way out.
_VALID_CLASSIFICATIONS = {"code-level", "config-level", "mixed", "none"}

# Bound the free-text fields so a verbose report can't produce an oversized row
# or a bloated notification-bell payload. Diagnosis stays generous (it backs the
# detail view); title/excerpt are short by design.
_MAX_TITLE = 200
_MAX_EXCERPT = 500
_MAX_DIAGNOSIS = 20_000


def _classification(report_data: dict[str, Any]) -> str:
    handoff = report_data.get("handoff") or {}
    raw = str(handoff.get("classification") or "none").replace("_", "-")
    return raw if raw in _VALID_CLASSIFICATIONS else "none"


def _title(report_data: dict[str, Any]) -> str:
    """A short headline for the Alerts row.

    Prefer the top root cause's one-sentence summary; fall back to the alert
    name so a no-root-cause report still gets a meaningful title.
    """
    result = report_data.get("result") or {}
    root_causes = result.get("root_causes") or []
    if root_causes and root_causes[0].get("summary"):
        title = str(root_causes[0]["summary"])
    else:
        title = str((report_data.get("alert_context") or {}).get("alert_name") or "RCA report")
    return title[:_MAX_TITLE]


def _render_diagnosis(report_data: dict[str, Any]) -> str:
    """Render the report body as Markdown for the console's detail/stepper view.

    aep-api stores this verbatim in the ``diagnosis`` text column; the console
    renders it in the "Alert Received" stage. We build it from the structured
    result rather than dumping raw JSON so it reads well in the UI.
    """
    result = report_data.get("result") or {}
    lines: list[str] = []

    if summary := report_data.get("summary"):
        lines.append(str(summary))
        lines.append("")

    root_causes = result.get("root_causes") or []
    if root_causes:
        lines.append("## Root causes")
        for i, rc in enumerate(root_causes, 1):
            conf = rc.get("confidence")
            header = f"{i}. {rc.get('summary', '')}"
            if conf:
                header += f" _(confidence: {conf})_"
            lines.append(header)
            if analysis := rc.get("analysis"):
                lines.append(f"   {analysis}")
        lines.append("")

    if explanation := result.get("explanation"):
        # no_root_cause_identified branch
        lines.append("## Analysis")
        lines.append(str(explanation))
        lines.append("")

    recommendations = (result.get("recommendations") or {}).get("recommended_actions") or []
    if recommendations:
        lines.append("## Recommended actions")
        for action in recommendations:
            status = action.get("status")
            desc = action.get("description", "")
            lines.append(f"- {desc}" + (f" _({status})_" if status else ""))
            if rationale := action.get("rationale"):
                lines.append(f"  {rationale}")
        lines.append("")

    # Before the timeline on purpose: this string is truncated from the end, so
    # the decision has to outrank the supporting evidence.
    lines.extend(_render_handoff_decision(report_data))

    timeline = result.get("timeline") or []
    if timeline:
        lines.append("## Timeline")
        for ev in timeline:
            comp = f"[{ev['component']}] " if ev.get("component") else ""
            lines.append(f"- `{ev.get('timestamp', '')}` {comp}{ev.get('event', '')}")
        lines.append("")

    return "\n".join(lines).strip()[:_MAX_DIAGNOSIS]


def _render_handoff_decision(report_data: dict[str, Any]) -> list[str]:
    """Render the handoff's own reasoning as Markdown lines.

    The handoff decides whether an incident becomes coding-agent work, and on a
    DECLINE that decision is the most consequential thing on the page: no issue
    is filed, so nothing is ever dispatched (ADR-0017 — filing an issue IS the
    dispatch). That reasoning used to die here. ``rationale`` was published only
    inside the ``issue_number is not None`` branch of
    ``build_create_report_request`` — exactly the branch a decline does not take
    — and ``ruled_out`` / ``related_issues`` were never published at all. The
    console was left asserting "the handoff found no actionable remediation"
    directly beneath remediation's own list of code changes it wanted, with
    nothing to back it. Right or wrong, that reads as an incident the platform
    dropped.

    This goes into ``diagnosis`` rather than new API fields deliberately.
    ``diagnosis`` is already contracted as the full RCA + remediation content in
    Markdown and the console already renders it, so the reasoning reaches a
    human with no contract change, no migration, and no coordinated aep-api and
    console deploy. Structured fields belong in a later change that can surface
    this in the Issue Created stage, where the bare assertion actually lives.
    """
    handoff = report_data.get("handoff") or {}
    if not handoff:
        return []

    actions = ((report_data.get("result") or {}).get("recommendations") or {}).get(
        "recommended_actions"
    ) or []

    lines = ["## Handoff decision", ""]
    lines.append(f"**Classification:** {_classification(report_data)}")
    if handoff.get("created_issue_number") is None:
        lines.append("")
        lines.append("No issue was filed for this alert, so no coding agent was dispatched.")
    lines.append("")

    if rationale := handoff.get("rationale"):
        lines.append(str(rationale))
        lines.append("")

    if ruled_out := handoff.get("ruled_out") or []:
        lines.append("### Recommended actions ruled out")
        for entry in ruled_out:
            index = entry.get("index")
            description = ""
            if isinstance(index, int) and 0 <= index < len(actions):
                description = str(actions[index].get("description") or "")
            lines.append(f"- **{description or f'action[{index}]'}**")
            lines.append(
                f"  _{entry.get('reason') or 'unspecified'}_ — {entry.get('justification') or ''}"
            )
        lines.append("")

    if related := handoff.get("related_issues") or []:
        lines.append("### Related issues")
        for issue in related:
            label = f"#{issue.get('number')} {issue.get('title') or ''}".strip()
            url = str(issue.get("url") or "")
            lines.append(f"- [{label}]({url})" if url else f"- {label}")
        lines.append("")

    return lines


def build_create_report_request(report_data: dict[str, Any]) -> dict[str, Any]:
    """Map the agent's report dict onto aep-api's CreateRcaAgentReportRequest.

    ``report_data`` is ``RCAReport.model_dump()`` after the remediation and
    handoff stages have run, so ``handoff`` (when present) carries the created
    issue + dispatch state. The org is intentionally omitted: aep-api binds it
    from the authenticated token.
    """
    alert_context = report_data.get("alert_context") or {}
    handoff = report_data.get("handoff") or {}

    payload: dict[str, Any] = {
        "project": alert_context.get("project", ""),
        "component": alert_context.get("component", ""),
        "title": _title(report_data),
        "summary": str(report_data.get("summary") or ""),
        "classification": _classification(report_data),
        "diagnosis": _render_diagnosis(report_data),
    }

    issue_number = handoff.get("created_issue_number")
    if issue_number is not None:
        payload["issueNumber"] = issue_number
        payload["issueUrl"] = handoff.get("created_issue_url") or ""
        # The handoff rationale is the best short "why this issue exists" excerpt
        # we have (HandoffResult carries no issue title/body).
        if rationale := handoff.get("rationale"):
            payload["issueExcerpt"] = str(rationale)[:_MAX_EXCERPT]
        payload["dispatched"] = bool(handoff.get("adopted"))
        # Which attempt this is. Sent only when AE established it, because 0 on
        # the wire means "unknown" and claiming a first attempt on behalf of an
        # answer that did not carry one would be a guess. Above 1 it tells
        # whoever is triaging that a fix already merged for this incident did
        # not work — a different situation from a new bug.
        if recurrence := handoff.get("recurrence"):
            payload["recurrence"] = int(recurrence)

    return payload


def should_publish_report(report_data: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether this completed report should be published to aep-api.

    Skips reports whose handoff **deduped** onto an already-open issue: that
    incident was already published by the earlier run that *created* the issue
    (and which may have dispatched a coding agent). Publishing again would add a
    second, misleading row whose ``dispatched``/``deployed`` snapshot is
    ``false`` — masking the real state on the console's Coding Handover / Verify
    Fix stages. The authoritative row is the creating run's; aep-api's read-time
    task correlation keeps its dispatch/deploy state live.

    Returns ``(publish, reason)`` — ``reason`` is a human-readable skip cause,
    empty when publishing.
    """
    handoff = report_data.get("handoff") or {}
    if handoff.get("deduped"):
        return (
            False,
            "handoff deduped onto an existing issue (already reported by its creating run)",
        )
    return True, ""


async def publish_rca_report(report_data: dict[str, Any], auth: httpx.Auth) -> str | None:
    """POST a completed RCA report to aep-api. Returns the created report id.

    Raises on transport/HTTP errors so the caller can log them — publishing is
    best-effort and must never fail the analysis (the report is already stored
    locally), so ``run_analysis`` wraps this in a try/except.
    """
    url = f"{settings.rca_reports_api_base}{_REPORTS_PATH}"
    payload = build_create_report_request(report_data)

    async with httpx.AsyncClient(
        verify=not settings.tls_insecure_skip_verify,
        timeout=httpx.Timeout(15.0, connect=5.0),
    ) as client:
        response = await client.post(url, json=payload, auth=auth)
        response.raise_for_status()
        created = response.json()

    report_id = created.get("id") if isinstance(created, dict) else None
    logger.debug("Published RCA report to aep-api: id=%s", report_id)
    return report_id
