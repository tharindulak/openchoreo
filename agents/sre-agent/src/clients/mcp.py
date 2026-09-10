# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient, StreamableHttpConnection

from src.config import settings

logger = logging.getLogger(__name__)


def _httpx_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        verify=not settings.tls_insecure_skip_verify,
    )


def build_connections(
    auth: httpx.Auth, handoff_headers: dict[str, str] | None = None
) -> dict[str, StreamableHttpConnection]:
    """The MCP servers this agent talks to, and what each is told.

    `handoff_headers` is this incident's identity (project, component, error
    signature) under the receiving platform's own header names. It is applied to
    the HANDOFF connection only: the other two servers have no business knowing
    which incident a query belongs to.
    """
    base: StreamableHttpConnection = {
        "transport": "streamable_http",
        "url": settings.observer_mcp_url,
        "httpx_client_factory": _httpx_client_factory,
        "auth": auth,
    }
    connections: dict[str, StreamableHttpConnection] = {
        "observability": {**base, "url": settings.observer_mcp_url},
        "openchoreo": {**base, "url": settings.openchoreo_mcp_url},
    }
    if settings.handoff_enabled:
        handoff: StreamableHttpConnection = {**base, "url": settings.handoff_mcp_url}
        if handoff_headers:
            handoff["headers"] = handoff_headers
        connections["handoff"] = handoff
    return connections


class MCPClient:
    def __init__(self, auth: httpx.Auth, handoff_headers: dict[str, str] | None = None) -> None:
        connections = build_connections(auth, handoff_headers)
        self._client = MultiServerMCPClient(connections)
        logger.debug("Initialized MCP client with servers: %s", list(connections))

    async def get_tools(self, *, server_name: str | None = None) -> list[BaseTool]:
        # Retry with exponential backoff: get_tools() opens fresh connections
        # to all configured MCP servers in a task group, so a single transient
        # failure (a server slow under load, an OAuth token fetch timing out on
        # a CPU-starved node) fails the whole call. Without a retry that killed
        # entire handoff runs — no issue, no dispatch — even though the very
        # next attempt would have succeeded. Retry the whole call so one flaky
        # server doesn't abort the run.
        attempts = max(1, settings.mcp_get_tools_max_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._client.get_tools(server_name=server_name)
            except Exception as e:  # noqa: BLE001 — retry any connection/task-group error
                last_exc = e
                if attempt < attempts:
                    backoff = settings.mcp_get_tools_retry_backoff_seconds * attempt
                    logger.warning(
                        "MCP get_tools failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt,
                        attempts,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)

        logger.error(
            "Failed to fetch tools from MCP client after %d attempts: %s",
            attempts,
            last_exc,
            exc_info=True,
        )
        raise RuntimeError(f"Failed to fetch tools from MCP client: {last_exc}") from last_exc
