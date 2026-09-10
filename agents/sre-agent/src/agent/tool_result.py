# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Best-effort decoding of a tool call's result content into plain data.

Shared by every middleware that reads a tool's answer without being the tool
itself: LogCaptureMiddleware reads query_component_logs's raw entries,
ToolCallRecorder reads whichever tool the handoff stage's model called last.
Neither of them owns a schema for what they are reading, so this never raises
— an unparseable value comes back unchanged, and the caller decides what, if
anything, it means.
"""

import json
from typing import Any


def parse_tool_result(raw: Any) -> Any:
    """Decode a tool result's content into whatever JSON value it holds.

    Handles the three shapes an adapter hands a middleware: an already-parsed
    dict/list, a JSON string, or the SDK's own content-block list
    (``[{"type": "text", "text": "..."}]``). Returns the original value
    unchanged when it is a string that is not valid JSON — never raises.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                return parse_tool_result(block.get("text", ""))
        return raw
    return raw
