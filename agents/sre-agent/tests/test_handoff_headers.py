# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""build_handoff_headers is the one mechanism that carries any deterministic,
model-independent fact onto the handoff connection — incident identity and
the remediation agent's action statuses alike. It does not know what any of
its field names MEAN; config says which fields become which headers."""

from src.agent.handoff_headers import build_handoff_headers


def test_configured_fields_become_the_configured_header_names():
    headers = build_handoff_headers(
        {"project": "p1", "component": "c1"},
        {"project": "X-Tracker-Project", "component": "X-Tracker-Unit"},
    )
    assert headers == {"X-Tracker-Project": "p1", "X-Tracker-Unit": "c1"}


def test_a_field_with_no_configured_header_is_omitted():
    headers = build_handoff_headers({"project": "p1", "component": "c1"}, {"project": "X-P"})
    assert headers == {"X-P": "p1"}


def test_a_configured_field_absent_from_context_is_omitted():
    headers = build_handoff_headers({"project": "p1"}, {"project": "X-P", "component": "X-C"})
    assert headers == {"X-P": "p1"}


def test_a_none_value_is_omitted_not_sent_as_the_string_none():
    headers = build_handoff_headers({"signature": None}, {"signature": "X-S"})
    assert headers == {}


def test_a_structured_value_is_json_encoded():
    headers = build_handoff_headers(
        {"action_statuses": ["suggested", None]}, {"action_statuses": "X-Statuses"}
    )
    assert headers == {"X-Statuses": '["suggested", null]'}


def test_a_string_value_is_sent_as_is_not_json_quoted():
    headers = build_handoff_headers({"project": "p1"}, {"project": "X-P"})
    assert headers["X-P"] == "p1"


def test_no_header_map_at_all_produces_no_headers():
    assert build_handoff_headers({"project": "p1"}, {}) == {}
