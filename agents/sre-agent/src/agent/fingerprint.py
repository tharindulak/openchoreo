# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic error fingerprint for the RCA handoff dedupe key.

The handoff dedupe key used to be component-only (``sre-rca/<component>``), so
any two incidents on the same service folded onto one open issue regardless of
root cause — a genuinely different bug was silently suppressed until the first
issue was closed. Appending a fingerprint of the *triggering error signature*
lets distinct root causes each get their own issue while identical recurrences
still dedupe.

The fingerprint MUST be deterministic: the same underlying error has to hash to
the same value across runs. We therefore derive it from the raw error log lines
(normalised to a template), never from the LLM's free-text root-cause summary,
which varies run to run and would defeat dedup. Normalisation masks the
per-occurrence noise (timestamps, ids, numbers, durations) so repeated
occurrences of one error collapse to a single template while genuinely
different errors keep distinct templates.
"""

import hashlib
import re
from collections import Counter
from typing import Any

# Volatile-token normalisers, applied in order. Order matters: the more specific
# patterns (timestamps, uuids, ids) run before the generic bare-number pattern so
# they are masked as a unit rather than digit-by-digit.
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    # ISO-8601 timestamps (2026-07-20T06:44:27.039Z, "2026-07-20 06:44:27").
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}\S*", re.IGNORECASE), "<ts>"),
    # UUIDs.
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    # key=value correlation ids (requestId=..., trace_id=...).
    (
        re.compile(r"\b(request_?id|correlation_?id|trace_?id|span_?id)=\S+", re.IGNORECASE),
        r"\1=<id>",
    ),
    # bare req-/req_ correlation tokens (req-abc123).
    (re.compile(r"\breq[-_][0-9a-z]+\b", re.IGNORECASE), "<reqid>"),
    # IPv4 with optional port.
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<addr>"),
    # hex addresses/ids (0x1f3a).
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    # durations and sizes (8123ms, 5s, 12mb).
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|us|ns|kb|mb|gb|b)\b", re.IGNORECASE), "<dur>"),
    # any remaining bare numbers.
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    # quoted values (filenames, urls, payloads).
    (re.compile(r"\"[^\"]*\"|'[^']*'"), "<q>"),
    # collapse whitespace runs.
    (re.compile(r"\s+"), " "),
]

# Length of the hex fingerprint appended to the dedupe key. 10 hex chars = 40
# bits, ample to avoid accidental collisions between the handful of distinct
# error signatures a single component realistically produces.
_FINGERPRINT_LEN = 10


def normalize_log_line(line: str) -> str:
    """Reduce a log line to a stable template by masking volatile tokens."""
    s = line.strip().lower()
    for pattern, repl in _NORMALIZERS:
        s = pattern.sub(repl, s)
    return s.strip()


def _collect_error_log_lines(report_data: dict[str, Any]) -> list[str]:
    """Pull log-evidence lines from the RCA report, preferring ERROR level.

    Walks ``result.root_causes[].supporting_findings[].evidence`` for evidence
    of ``type == "log"``. Returns ERROR-level lines when any exist, otherwise
    every log line found (so a report that only carried WARN/INFO evidence still
    yields a signature rather than falling through to the alert fallback).
    """
    error_lines: list[str] = []
    other_lines: list[str] = []
    result = report_data.get("result") or {}
    for root_cause in result.get("root_causes") or []:
        for finding in root_cause.get("supporting_findings") or []:
            evidence = finding.get("evidence") or {}
            if evidence.get("type") != "log":
                continue
            for log_line in evidence.get("log_lines") or []:
                message = str(log_line.get("log") or "").strip()
                if not message:
                    continue
                if str(log_line.get("level") or "").upper() == "ERROR":
                    error_lines.append(message)
                else:
                    other_lines.append(message)
    return error_lines or other_lines


def _alert_signature(report_data: dict[str, Any]) -> str:
    """Fallback signature for metric/trace alerts that carry no usable log line.

    Coarser than a log template (it keys on the alert's own source), but bounded
    per alert rule so distinct metric/trace alerts on the same component still
    separate.
    """
    alert = report_data.get("alert_context") or {}
    parts = [
        str(alert.get("source_type") or ""),
        str(alert.get("source_metric") or alert.get("source_query") or ""),
        str(alert.get("alert_name") or ""),
    ]
    return "|".join(part for part in parts if part)


def error_fingerprint(report_data: dict[str, Any] | None) -> str | None:
    """Return a short, deterministic fingerprint of the incident's root cause.

    Primary source is the dominant normalised ERROR log template; falls back to
    the alert's source signature for metric/trace alerts. Returns ``None`` when
    there is nothing to fingerprint — the caller then uses the component-only key
    (preserving the previous behaviour for that incident rather than filing an
    unkeyed issue).
    """
    if not report_data:
        return None

    lines = _collect_error_log_lines(report_data)
    if lines:
        templates = [t for t in (normalize_log_line(line) for line in lines) if t]
        if not templates:
            return None
        # Pick the dominant template; break ties lexicographically so the choice
        # is independent of the order the LLM happened to list the lines in.
        counts = Counter(templates)
        top = max(counts.values())
        template = min(t for t, count in counts.items() if count == top)
    else:
        template = normalize_log_line(_alert_signature(report_data))

    if not template:
        return None
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]
