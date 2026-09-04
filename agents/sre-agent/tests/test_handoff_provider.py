# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the handoff provider descriptor.

A descriptor is deploy-time config, and config fails QUIETLY. That is the whole
risk this file guards: a missing dedupe-key name would not crash anything, it
would just file a fresh issue for every recurrence of every incident, and nobody
would notice until a human saw the duplicates. So every required name is
refused rather than defaulted, and it is refused at load time.
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.agent.handoff_provider import (
    HandoffProviderError,
    load_provider,
    parse_provider,
)

VALID = {
    "tools": {"create_issue": "ae_create_issue", "search_related": "ae_search_related_issues"},
    "incident_headers": {
        "project": "X-Tracker-Project",
        "component": "X-Tracker-Unit",
        "signature": "X-Tracker-Signature",
    },
    "answer_fields": {
        "issue_number": "number",
        "issue_url": "url",
        "already_filed": "deduped",
        "facts": ["adopted", "recurrence"],
    },
}


def test_a_complete_descriptor_parses():
    p = parse_provider(VALID)

    assert p.create_issue_tool == "ae_create_issue"
    assert p.tools == {"ae_create_issue", "ae_search_related_issues"}
    assert (p.header_project, p.header_component, p.header_signature) == (
        "X-Tracker-Project",
        "X-Tracker-Unit",
        "X-Tracker-Signature",
    )
    assert p.answer_facts == ("adopted", "recurrence")


def test_the_receiver_names_its_own_identity_headers():
    provider = parse_provider(
        {
            "tools": {"create_issue": "file", "search_related": "find"},
            "incident_headers": {
                "project": "X-Tracker-Project",
                "component": "X-Tracker-Unit",
                "signature": "X-Tracker-Signature",
            },
            "answer_fields": {"issue_number": "ref"},
        }
    )

    assert provider.incident_headers("proj", "svc1", "a1b2") == {
        "X-Tracker-Project": "proj",
        "X-Tracker-Unit": "svc1",
        "X-Tracker-Signature": "a1b2",
    }


def test_an_identity_the_agent_does_not_have_is_omitted_not_blank():
    provider = parse_provider(
        {
            "tools": {"create_issue": "file", "search_related": "find"},
            "incident_headers": {
                "project": "X-P",
                "component": "X-C",
                "signature": "X-S",
            },
        }
    )

    # A blank header is a value the receiver would have to validate and reject;
    # an absent one falls back cleanly to the caller's own arguments.
    assert provider.incident_headers("proj", None, None) == {"X-P": "proj"}


def test_a_descriptor_without_identity_headers_is_refused_at_startup():
    with pytest.raises(HandoffProviderError):
        parse_provider({"tools": {"create_issue": "file", "search_related": "find"}})


# Each of these, missing, produces a DIFFERENT silent failure — an unwrapped
# tool, or an incident identity that never reaches the receiver. None of them
# raise on their own.
@pytest.mark.parametrize(
    "section,key",
    [
        ("tools", "create_issue"),
        ("tools", "search_related"),
        ("incident_headers", "project"),
        ("incident_headers", "component"),
        ("incident_headers", "signature"),
    ],
)
def test_every_required_name_is_refused_when_missing(section, key):
    raw = json.loads(json.dumps(VALID))
    del raw[section][key]

    with pytest.raises(HandoffProviderError, match=f"{section}.{key}"):
        parse_provider(raw)


def test_a_blank_name_is_as_bad_as_a_missing_one():
    raw = json.loads(json.dumps(VALID))
    raw["incident_headers"]["project"] = "   "

    with pytest.raises(HandoffProviderError, match="project"):
        parse_provider(raw)


def test_a_whole_section_missing_is_named():
    for section in ("tools", "incident_headers"):
        raw = json.loads(json.dumps(VALID))
        del raw[section]
        with pytest.raises(HandoffProviderError, match=section):
            parse_provider(raw)


# The answer names have defaults because getting one wrong degrades visibly — a
# missing issue number shows up as "no issue filed" on the report — whereas a
# missing REQUIRED header is invisible. Different risk, different treatment.
def test_answer_names_fall_back_but_required_ones_never_do():
    raw = json.loads(json.dumps(VALID))
    del raw["answer_fields"]

    p = parse_provider(raw)
    assert (p.answer_issue_number, p.answer_issue_url, p.answer_already_filed) == (
        "number",
        "url",
        "deduped",
    )
    assert p.answer_facts == ()


def test_facts_must_be_strings():
    raw = json.loads(json.dumps(VALID))
    raw["answer_fields"]["facts"] = [{"nope": True}]
    with pytest.raises(HandoffProviderError, match="facts"):
        parse_provider(raw)


def test_a_descriptor_that_is_not_an_object_is_refused():
    with pytest.raises(HandoffProviderError, match="must be an object"):
        parse_provider(["tools"])  # type: ignore[arg-type]


# The two file-level failures. Both name the path and say who ships the file,
# because whoever hits this is looking at a pod that started fine and a handoff
# that did nothing.
def test_a_missing_file_says_where_it_should_be_and_who_ships_it():
    with tempfile.TemporaryDirectory() as d:
        missing = str(Path(d) / "nope.json")
        with pytest.raises(HandoffProviderError) as e:
            load_provider(missing)
        assert missing in str(e.value)
        assert "HANDOFF_PROVIDER_FILE" in str(e.value)


def test_malformed_json_is_refused_not_ignored():
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "provider.json"
        bad.write_text("{not json")
        with pytest.raises(HandoffProviderError, match="not valid JSON"):
            load_provider(str(bad))


def test_a_real_file_round_trips():
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "provider.json"
        good.write_text(json.dumps(VALID))
        assert load_provider(str(good)).create_issue_tool == "ae_create_issue"
