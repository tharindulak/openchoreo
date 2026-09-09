# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the report sink — the seam a downstream system plugs into.

What has to hold is mostly about what the sink does NOT do. It used to be an
AEP-specific publisher that spelled another system's endpoint path, mapped its
field names, renamed an enum and rendered its console's Markdown; all of that now
lives on the receiving side. So these tests pin that the report goes out as this
agent models it, unmapped, and that a failure to publish never costs the
analysis.
"""

import json

import httpx
import pytest

from src.clients.sink import WebhookReportSink, get_report_sink, should_publish_report
from src.config import settings

REPORT = {
    "summary": "service1 timed out",
    "alert_context": {"project": "demohello", "component": "demohello-service1"},
    "result": {"root_causes": [{"summary": "service2 is slow", "confidence": "high"}]},
    "handoff": {"classification": "code_level", "created_issue_number": 41},
}


def _mock_transport(monkeypatch, handler):
    """Point the sink's httpx client at a local handler.

    The real AsyncClient is captured BEFORE patching — patching the name and
    then calling it is a lambda that calls itself.
    """
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler)),
    )


async def test_the_report_goes_out_unmapped(monkeypatch):
    """The whole point: what the receiver gets is the agent's own document.

    A sink that renamed a field or flattened a section would be carrying a
    contract it does not own — which is exactly what was removed.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "row-7"})

    _mock_transport(monkeypatch, handler)

    sink = WebhookReportSink("https://receiver.invalid/reports")
    report_id = await sink.publish(REPORT, httpx.BasicAuth("u", "p"))

    assert report_id == "row-7"
    assert seen["url"] == "https://receiver.invalid/reports"
    assert seen["body"] == {"report": REPORT}, "the report must arrive as-is"


async def test_a_receiver_that_returns_no_id_is_not_an_error(monkeypatch):
    _mock_transport(monkeypatch, lambda r: httpx.Response(204))
    sink = WebhookReportSink("https://receiver.invalid/reports")
    assert await sink.publish(REPORT, httpx.BasicAuth("u", "p")) is None


async def test_a_rejected_publish_raises_so_the_caller_can_log_it(monkeypatch):
    """It must NOT be swallowed here. A sink that silently drops reports is
    indistinguishable from one that is working; the caller decides that a
    publish failure is survivable, because the report is already stored."""
    _mock_transport(monkeypatch, lambda r: httpx.Response(500, text="nope"))
    sink = WebhookReportSink("https://receiver.invalid/reports")
    with pytest.raises(httpx.HTTPStatusError):
        await sink.publish(REPORT, httpx.BasicAuth("u", "p"))


def test_no_sink_configured_is_the_default(monkeypatch):
    """None, not a no-op sink: "nothing is configured" and "a sink ran" are
    logged differently, so a null object would hide a misconfigured deploy."""
    monkeypatch.setattr(settings, "report_sink", "")
    assert get_report_sink() is None


def test_the_webhook_sink_is_selected_by_name(monkeypatch):
    monkeypatch.setattr(settings, "report_sink", "webhook")
    monkeypatch.setattr(settings, "report_sink_url", "https://receiver.invalid/reports")
    assert isinstance(get_report_sink(), WebhookReportSink)


def test_an_unknown_sink_fails_loudly(monkeypatch):
    """A typo must not degrade to publishing nowhere. Reports vanishing quietly
    is the failure this whole path exists to avoid."""
    monkeypatch.setattr(settings, "report_sink", "kafka")
    with pytest.raises(ValueError, match="Unknown report sink"):
        get_report_sink()


def test_a_deduped_handoff_is_not_published_twice():
    """The creating run already published this incident, and its row carries the
    live dispatch state. A second row would read as unworked while a coding
    agent is on it."""
    publish, reason = should_publish_report({"handoff": {"deduped": True}})
    assert publish is False
    assert "deduped" in reason


def test_everything_else_publishes_including_a_decline():
    """A decline is exactly what somebody wants to read when wondering why no
    issue was filed, so it must not be withheld."""
    for handoff in ({}, {"deduped": False}, {"classification": "none", "rationale": "no defect"}):
        publish, reason = should_publish_report({"handoff": handoff})
        assert publish is True, handoff
        assert reason == ""
