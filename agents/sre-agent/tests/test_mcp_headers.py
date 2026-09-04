# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Incident identity goes to the handoff receiver and nowhere else.

The observability and OpenChoreo servers have no business knowing which incident
a query belongs to, and a header set on every connection would hand one system's
identifiers to two unrelated ones.
"""

import httpx

from src.clients.mcp import build_connections


def test_only_the_handoff_connection_carries_the_identity_headers(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "handoff_enabled", True)
    headers = {"X-AEP-Incident-Component": "service1"}

    connections = build_connections(httpx.BasicAuth("u", "p"), handoff_headers=headers)

    assert connections["handoff"]["headers"] == headers
    assert "headers" not in connections["observability"]
    assert "headers" not in connections["openchoreo"]


def test_no_identity_means_no_header_key_at_all(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "handoff_enabled", True)

    connections = build_connections(httpx.BasicAuth("u", "p"))

    assert "headers" not in connections["handoff"]
