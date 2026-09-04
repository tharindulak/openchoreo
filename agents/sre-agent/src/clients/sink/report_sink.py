# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Where a completed report goes BESIDES this agent's own backend.

``report_backend`` is the agent's own store and powers its own REST/chat API. A
sink is the other direction: a downstream system that wants to know an analysis
finished — a platform console, a ticketing system, an event bus.

The split from the backend is deliberate. A backend is queried by this agent and
must answer reads; a sink is written to and never read back, so it has one
method and no lifecycle. Conflating them would force every downstream
integration to implement three query methods it has no answer for.

**The sink is handed the report as this agent models it, and nothing else.** It
does not map field names, spell another system's endpoint path, rename an enum,
or render that system's presentation format. Every one of those is a fact about
the RECEIVER, and a receiver is the only thing that can be trusted to know
them — an agent that encodes them ends up carrying a contract it does not own
and cannot test, and every change to that contract becomes a change to this
repository. The receiver maps what it is sent.

Publishing is best-effort by construction: the report is already durable in the
agent's own backend before a sink is ever called, so a sink that is down, slow
or misconfigured must never cost the analysis. Callers wrap this accordingly.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


def should_publish_report(report_data: dict[str, Any]) -> tuple[bool, str]:
    """Whether a completed report should reach a downstream sink.

    One rule, and it exists because a duplicate row is worse than a missing one.
    A handoff that **deduped** folded this incident onto an issue an earlier run
    already filed — and that earlier run already published its own report. A
    second publish would add a row whose dispatch state reads false while the
    authoritative row's is live, so a human triaging would see the incident as
    unworked when a coding agent is on it.

    Everything else publishes, including a decline: a report saying "no code
    change is needed, and here is why per action" is exactly what somebody needs
    when they are wondering why nothing was filed.

    Returns ``(publish, reason)``; ``reason`` is a human-readable skip cause and
    is empty when publishing.
    """
    handoff = report_data.get("handoff") or {}
    if handoff.get("deduped"):
        return (
            False,
            "handoff deduped onto an existing issue (already reported by its creating run)",
        )
    return True, ""


class ReportSink(ABC):
    """A downstream destination for completed reports."""

    @abstractmethod
    async def publish(self, report: dict[str, Any], auth: httpx.Auth) -> str | None:
        """Send one completed report. Returns the receiver's id for it when it
        gives one, else None.

        Raises on transport or protocol failure so the caller can log it. Do NOT
        swallow errors here: a sink that silently drops reports is
        indistinguishable from one that is working.
        """


class WebhookReportSink(ReportSink):
    """POSTs the report as JSON to a configured URL.

    The whole implementation of "publish to something over HTTP". It carries the
    caller's auth through unchanged — the agent's service-account credentials
    are the same ones the rest of its outbound calls use, so a sink needs no auth
    plumbing of its own.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    async def publish(self, report: dict[str, Any], auth: httpx.Auth) -> str | None:
        async with httpx.AsyncClient(
            verify=not settings.tls_insecure_skip_verify,
            # Bounded so a wedged receiver cannot hold an analysis's tail open.
            timeout=httpx.Timeout(15.0, connect=5.0),
        ) as client:
            response = await client.post(self._url, json={"report": report}, auth=auth)
            response.raise_for_status()
            body = response.json() if response.content else None

        report_id = body.get("id") if isinstance(body, dict) else None
        logger.debug("Published report to %s: id=%s", self._url, report_id)
        return report_id
