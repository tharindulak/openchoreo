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

    timeline = result.get("timeline") or []
    if timeline:
        lines.append("## Timeline")
        for ev in timeline:
            comp = f"[{ev['component']}] " if ev.get("component") else ""
            lines.append(f"- `{ev.get('timestamp', '')}` {comp}{ev.get('event', '')}")
        lines.append("")

    return "\n".join(lines).strip()[:_MAX_DIAGNOSIS]


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
        payload["dispatched"] = bool(handoff.get("dispatch_run_name"))

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
        return False, "handoff deduped onto an existing issue (already reported by its creating run)"
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
