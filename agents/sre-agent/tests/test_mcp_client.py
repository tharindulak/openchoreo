# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

import pytest

from src.clients.mcp import MCPClient


class _FakeMultiServerClient:
    def __init__(self, connections):
        self.connections = connections
        self.seen_server_name = "NOT CALLED"

    async def get_tools(self, *, server_name=None):
        self.seen_server_name = server_name
        return [f"tool-from-{server_name or 'all'}"]


@pytest.mark.asyncio
async def test_get_tools_passes_server_name_through(monkeypatch):
    fake = None

    def _fake_ctor(connections):
        nonlocal fake
        fake = _FakeMultiServerClient(connections)
        return fake

    monkeypatch.setattr("src.clients.mcp.MultiServerMCPClient", _fake_ctor)

    import httpx

    client = MCPClient(auth=httpx.BasicAuth("u", "p"))
    result = await client.get_tools(server_name="handoff")

    assert fake.seen_server_name == "handoff"
    assert result == ["tool-from-handoff"]


@pytest.mark.asyncio
async def test_get_tools_with_no_server_name_gets_everything(monkeypatch):
    fake = None

    def _fake_ctor(connections):
        nonlocal fake
        fake = _FakeMultiServerClient(connections)
        return fake

    monkeypatch.setattr("src.clients.mcp.MultiServerMCPClient", _fake_ctor)

    import httpx

    client = MCPClient(auth=httpx.BasicAuth("u", "p"))
    result = await client.get_tools()

    assert fake.seen_server_name is None
    assert result == ["tool-from-all"]
