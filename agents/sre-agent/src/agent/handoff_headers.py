# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The one mechanism that carries a deterministic, model-independent fact onto
the handoff MCP connection — as opposed to a tool-call argument, which the
model fills in and could therefore restate wrong.

Incident identity (project, component, error signature) and the remediation
agent's own action statuses are the same SHAPE of fact: computed by this
agent's own code, never the model's, and load-bearing for what the receiver
derives from them. Both ride this one path. This module does not know what
"project" or "action_statuses" MEAN to whatever receives them — only that
config says a given field becomes a given header name. See
``src.config.Settings.handoff_header_map``.
"""

import json
from typing import Any


def build_handoff_headers(
    context_values: dict[str, Any], header_map: dict[str, str]
) -> dict[str, str]:
    """Render configured context fields as request headers.

    A field with no configured header, or a context value that is absent or
    ``None``, is OMITTED rather than sent blank or as the literal string
    "None" — a receiver has to validate and reject a blank value, while an
    absent header falls back cleanly to whatever the receiver's own
    tool-argument fallback is.
    """
    headers: dict[str, str] = {}
    for field, header_name in header_map.items():
        if field not in context_values:
            continue
        value = context_values[field]
        if value is None:
            continue
        headers[header_name] = value if isinstance(value, str) else json.dumps(value)
    return headers
