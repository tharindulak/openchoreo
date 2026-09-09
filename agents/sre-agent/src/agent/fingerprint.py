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
the same value across runs. It is derived from the RAW log entries
``query_component_logs`` returned during the RCA run (captured verbatim by
``LogCaptureMiddleware``), never from the model's own structured findings —
measured live, the same defect triggered three times produced three different
fingerprints, because which lines the model chose to cite in
``supporting_findings[].evidence`` varies run to run the way any model output
does, even though the observability plane returned the same lines each time.
Normalisation masks the per-occurrence noise (timestamps, ids, numbers,
durations) so repeated occurrences of one error collapse to a single template
while genuinely different errors keep distinct templates.
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


def _error_lines(raw_log_lines: list[dict[str, Any]]) -> list[str]:
    """Lines matching what the alert rule itself watches for.

    The rule matches a substring of the RAW pod log line, ``error``,
    case-insensitively, with no severity filter — a WARN line containing the
    word fires it, an ERROR line that does not contain it never does. The
    observability plane hands this stage that raw line already split into
    `level` and `log` (message), so reproducing the rule's match means
    checking BOTH: `level=ERROR` is itself literal text in the line the rule
    saw, and is normally where "error" comes from — measured on this
    project's own verified defect, whose message text
    ("failed to compute ageInDays for catalog item") carries no "error"
    substring anywhere, only `level=ERROR` does. The reverse also happens: an
    unrouted-path line has no severity of its own but says "error:" in the
    message. Checking only one field misses one of the two real cases this
    project has already produced.

    Falls back to every captured line when nothing matches, so a report whose
    evidence used a different watched token still yields a signature rather
    than nothing at all.
    """
    matched: list[str] = []
    other: list[str] = []
    for entry in raw_log_lines:
        message = str(entry.get("log") or "").strip()
        if not message:
            continue
        level = str(entry.get("level") or "")
        if "error" in message.lower() or "error" in level.lower():
            matched.append(message)
        else:
            other.append(message)
    return matched or other


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


def error_fingerprint(
    report_data: dict[str, Any] | None,
    raw_log_lines: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return a short, deterministic fingerprint of the incident's root cause.

    Primary source is the dominant normalised template among the RAW log lines
    `query_component_logs` returned during this run — code-observed, not
    model-curated. Falls back to the alert's source signature (from
    `report_data`) when nothing was ever captured, e.g. a metric/trace alert
    whose RCA never queried logs at all. Returns ``None`` when there is
    nothing to fingerprint either way — the caller then uses the
    component-only key (preserving the previous behaviour for that incident
    rather than filing an unkeyed issue).
    """
    lines = _error_lines(raw_log_lines or [])
    if lines:
        templates = [t for t in (normalize_log_line(line) for line in lines) if t]
        if not templates:
            return None
        # Pick the dominant template; break ties lexicographically so the
        # choice is independent of the order the raw entries arrived in.
        counts = Counter(templates)
        top = max(counts.values())
        template = min(t for t, count in counts.items() if count == top)
    elif report_data:
        template = normalize_log_line(_alert_signature(report_data))
    else:
        return None

    if not template:
        return None
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]
