# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the deterministic error fingerprint used by the handoff dedupe key.

Runnable with pytest, or directly (`python tests/test_fingerprint.py`) while the
repo has no pytest dependency wired up.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.fingerprint import (  # noqa: E402
    error_fingerprint,
    normalize_log_line,
)
from src.agent.handoff_logic import dedupe_key_for  # noqa: E402


def _log_report(*lines: tuple[str, str], source_type: str = "log") -> dict:
    """Build a minimal RCA report dict carrying the given (level, message) log
    lines as evidence under one root cause."""
    return {
        "alert_context": {"source_type": source_type, "source_query": "error"},
        "result": {
            "type": "root_cause_identified",
            "root_causes": [
                {
                    "supporting_findings": [
                        {
                            "evidence": {
                                "type": "log",
                                "log_lines": [
                                    {"level": lvl, "log": msg} for lvl, msg in lines
                                ],
                            }
                        }
                    ]
                }
            ],
        },
    }


def test_normalize_masks_volatile_tokens():
    a = normalize_log_line(
        "[ERROR] req-abc123 timeout waiting for service2 after 8123ms at 10.0.0.1:9091"
    )
    b = normalize_log_line(
        "[ERROR] req-def999 timeout waiting for service2 after 7998ms at 10.0.0.2:9091"
    )
    assert a == b, f"volatile tokens not normalised: {a!r} != {b!r}"
    assert "<reqid>" in a and "<dur>" in a and "<addr>" in a


def test_same_root_cause_same_fingerprint():
    # Same error, different request ids / durations → identical fingerprint.
    r1 = _log_report(("ERROR", "req-1 timeout waiting for service2 after 8000ms"))
    r2 = _log_report(("ERROR", "req-2 timeout waiting for service2 after 8123ms"))
    assert error_fingerprint(r1) == error_fingerprint(r2)


def test_different_root_cause_different_fingerprint():
    r1 = _log_report(("ERROR", "timeout waiting for service2 after 8000ms"))
    r2 = _log_report(("ERROR", "nil pointer dereference in handler.go"))
    assert error_fingerprint(r1) != error_fingerprint(r2)


def test_error_lines_preferred_over_lower_levels():
    # The ERROR line drives the fingerprint even when INFO/WARN lines are present.
    with_noise = _log_report(
        ("INFO", "starting request 12"),
        ("ERROR", "timeout waiting for service2 after 8000ms"),
        ("WARN", "retrying in 500ms"),
    )
    only_error = _log_report(("ERROR", "timeout waiting for service2 after 9999ms"))
    assert error_fingerprint(with_noise) == error_fingerprint(only_error)


def test_dominant_template_is_order_independent():
    r1 = _log_report(
        ("ERROR", "connection refused to db at 10.0.0.5:5432"),
        ("ERROR", "connection refused to db at 10.0.0.9:5432"),
        ("ERROR", "one-off blip 1"),
    )
    r2 = _log_report(
        ("ERROR", "one-off blip 2"),
        ("ERROR", "connection refused to db at 10.0.0.1:5432"),
        ("ERROR", "connection refused to db at 10.0.0.2:5432"),
    )
    assert error_fingerprint(r1) == error_fingerprint(r2)


def test_metric_alert_fallback():
    metric = {
        "alert_context": {
            "source_type": "metric",
            "source_metric": "cpu_utilization",
            "alert_name": "high-cpu",
        },
        "result": {"type": "root_cause_identified", "root_causes": []},
    }
    other = {
        "alert_context": {
            "source_type": "metric",
            "source_metric": "memory_utilization",
            "alert_name": "high-mem",
        },
        "result": {"type": "root_cause_identified", "root_causes": []},
    }
    fp_cpu = error_fingerprint(metric)
    assert fp_cpu is not None
    assert fp_cpu != error_fingerprint(other)


def test_empty_report_returns_none():
    assert error_fingerprint(None) is None
    assert error_fingerprint({}) is None


def test_dedupe_key_composition():
    assert dedupe_key_for("service1") == "sre-rca/service1"
    assert dedupe_key_for("service1", "abc123") == "sre-rca/service1/abc123"
    assert dedupe_key_for("service1", None) == "sre-rca/service1"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
