# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the deterministic error fingerprint used by the handoff dedupe key.

The source is the RAW log entries `query_component_logs` returned during the
RCA run — what `LogCaptureMiddleware` captures — never the model's own
structured findings. That distinction is the point: the model's citation of
which lines matter varies run to run even for the identical underlying error,
which is what made two triggers of one bug dedupe as if they were different
bugs before this fix.
"""

from src.agent.fingerprint import error_fingerprint, normalize_log_line


def _lines(*pairs: tuple[str, str]) -> list[dict]:
    """Raw log entries as LogCaptureMiddleware captures them: level + log."""
    return [{"level": level, "log": message} for level, message in pairs]


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
    # Same error, different request ids / durations -> identical fingerprint.
    r1 = _lines(("ERROR", "req-1 timeout waiting for service2 after 8000ms"))
    r2 = _lines(("ERROR", "req-2 timeout waiting for service2 after 8123ms"))
    assert error_fingerprint(None, r1) == error_fingerprint(None, r2)


def test_different_root_cause_different_fingerprint():
    r1 = _lines(("ERROR", "timeout waiting for service2 after 8000ms"))
    r2 = _lines(("ERROR", "nil pointer dereference in handler.go"))
    assert error_fingerprint(None, r1) != error_fingerprint(None, r2)


def test_deterministic_regardless_of_which_lines_a_run_happened_to_capture():
    # The bug this fixes: two runs of ONE underlying error used to fingerprint
    # differently because the model cited a different subset of what it saw.
    # The fix removes the model from this path entirely, so the property to
    # prove is that varying which OTHER lines came along for the ride, and in
    # what order, does not move the fingerprint — only the dominant error
    # template does. Mirrors this project's own verified defect: the message
    # text itself names no "error", only the level does.
    run_a = _lines(
        ("INFO", "handling request 41"),
        ("ERROR", "failed to compute ageInDays for catalog item"),
    )
    run_b = _lines(
        ("ERROR", "failed to compute ageInDays for catalog item"),
        ("INFO", "handling request 57"),
        ("DEBUG", "cache miss for key foo"),
    )
    assert error_fingerprint(None, run_a) == error_fingerprint(None, run_b)


def test_matches_on_either_the_level_or_the_message_text():
    # Two real cases this project has produced, both covered:
    #
    # (1) The message names no "error" — only level=ERROR does (this
    #     project's own verified defect: "failed to compute ageInDays").
    #     Checking level catches it regardless of what the message says.
    only_level_says_error = _lines(
        ("ERROR", "failed to compute ageInDays for catalog item"),
        ("INFO", "handled catalog-items request"),
    )
    same_message_different_level_label = _lines(
        ("SEVERE", "failed to compute ageInDays for catalog item"),
    )
    assert error_fingerprint(None, only_level_says_error) == error_fingerprint(
        None, same_message_different_level_label
    )

    # (2) The message says "error:" but the line has no severity of its own
    #     (a framework-default unrouted-path line). Checking the message text
    #     catches it even though `level` carries nothing.
    unrouted_path_line = _lines(
        ("", "error: no matching resource found for path : / , method : GET"),
    )
    assert error_fingerprint(None, unrouted_path_line) is not None


def test_falls_back_to_every_line_when_none_match_the_watched_token():
    with_noise = _lines(
        ("INFO", "starting request 12"),
        ("DEBUG", "retrying in 500ms"),
    )
    assert error_fingerprint(None, with_noise) is not None


def test_dominant_template_is_order_independent():
    r1 = _lines(
        ("ERROR", "connection refused to db at 10.0.0.5:5432"),
        ("ERROR", "connection refused to db at 10.0.0.9:5432"),
        ("ERROR", "one-off blip 1"),
    )
    r2 = _lines(
        ("ERROR", "one-off blip 2"),
        ("ERROR", "connection refused to db at 10.0.0.1:5432"),
        ("ERROR", "connection refused to db at 10.0.0.2:5432"),
    )
    assert error_fingerprint(None, r1) == error_fingerprint(None, r2)


def test_metric_alert_fallback_when_nothing_was_ever_captured():
    # A metric/trace alert whose RCA never called query_component_logs at
    # all — no raw lines exist to fingerprint from, so the alert's own
    # source identity is what's left.
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
    fp_cpu = error_fingerprint(metric, None)
    assert fp_cpu is not None
    assert fp_cpu != error_fingerprint(other, None)


def test_empty_report_and_no_lines_returns_none():
    assert error_fingerprint(None, None) is None
    assert error_fingerprint({}, None) is None
    assert error_fingerprint({}, []) is None
