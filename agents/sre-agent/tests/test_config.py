# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``Settings``'s validators and computed URL properties."""

import pytest

from src.config import Settings


def test_sqlite_default_uri_is_filled_in():
    s = Settings(report_backend="sqlite", sql_backend_uri="")
    assert s.sql_backend_uri == "sqlite+aiosqlite:///data/rca_reports.db"


def test_postgresql_requires_uri():
    with pytest.raises(ValueError, match="requires: sql_backend_uri"):
        Settings(report_backend="postgresql", sql_backend_uri="")


def test_scheme_must_match_backend():
    with pytest.raises(ValueError, match="must match report_backend"):
        Settings(report_backend="postgresql", sql_backend_uri="sqlite+aiosqlite:///x.db")


def test_postgresql_asyncpg_dialect_accepted():
    s = Settings(
        report_backend="postgresql",
        sql_backend_uri="postgresql+asyncpg://user:pass@host:5432/db",
    )
    assert s.report_backend == "postgresql"


def test_bare_postgres_scheme_is_rejected():
    # The URI must literally start with the report_backend value; the bare
    # ``postgres://`` alias is not normalized.
    with pytest.raises(ValueError, match="must match report_backend"):
        Settings(
            report_backend="postgresql",
            sql_backend_uri="postgres://user:pass@host:5432/db",
        )


def test_observer_mcp_url_appends_mcp_and_strips_slash():
    s = Settings(observer_api_url="http://observer:8080/")
    assert s.observer_mcp_url == "http://observer:8080/mcp"


def test_openchoreo_mcp_url_appends_mcp_and_strips_slash():
    s = Settings(openchoreo_api_url="http://api.example.com/")
    assert s.openchoreo_mcp_url == "http://api.example.com/mcp"


def test_authz_service_url_strips_trailing_slash():
    s = Settings(openchoreo_api_url="http://api.example.com/")
    assert s.authz_service_url == "http://api.example.com"


def test_handoff_requires_the_skill_mount():
    # The stage's whole playbook is the mounted skill, so without the mount
    # load_skills raises once per incident and the report records nothing —
    # a per-run failure where a startup failure belongs.
    with pytest.raises(ValueError, match="external_skills_dir"):
        Settings(
            handoff_enabled=True,
            handoff_api_url="http://aep:3401",
            handoff_header_map={"project": "X-AEP-Incident-Project"},
            external_skills_dir="",
        )


def test_handoff_requires_a_header_map():
    # An agent that files issues with no identity headers at all files them
    # against the wrong incident forever — this is the same class of silent
    # failure the old provider-descriptor requirement guarded.
    with pytest.raises(ValueError, match="handoff_header_map"):
        Settings(
            handoff_enabled=True,
            handoff_api_url="http://aep:3401",
            handoff_header_map={},
            external_skills_dir="/etc/rca-agent/skills",
        )


def test_handoff_accepts_a_complete_config():
    s = Settings(
        handoff_enabled=True,
        handoff_api_url="http://aep:3401",
        handoff_header_map={"project": "X-AEP-Incident-Project"},
        external_skills_dir="/etc/rca-agent/skills",
    )
    assert s.handoff_enabled


def test_handoff_header_map_parses_from_a_json_env_string():
    # ConfigMap data values are always strings; this field is populated from
    # HANDOFF_HEADER_MAP as a JSON object string in every real deployment.
    s = Settings(
        handoff_enabled=True,
        handoff_api_url="http://aep:3401",
        handoff_header_map='{"project": "X-AEP-Incident-Project"}',
        external_skills_dir="/etc/rca-agent/skills",
    )
    assert s.handoff_header_map == {"project": "X-AEP-Incident-Project"}


def test_a_malformed_header_map_is_refused_at_startup():
    with pytest.raises(ValueError):
        Settings(handoff_header_map="not json")


def test_empty_handoff_header_map_env_var_does_not_crash(monkeypatch):
    # EnvSettingsSource must not crash on HANDOFF_HEADER_MAP="" — it should
    # delegate to the validator which converts empty string to {} without
    # attempting to decode it as JSON. This is critical for unconfigured
    # deployments where a ConfigMap default is an empty string.
    monkeypatch.setenv("HANDOFF_HEADER_MAP", "")
    s = Settings()
    assert s.handoff_header_map == {}


def test_handoff_header_map_parses_valid_json_from_env_var(monkeypatch):
    # When HANDOFF_HEADER_MAP is set to a valid JSON object string in the
    # environment (the standard ConfigMap pattern), pydantic_settings should
    # hand the raw string to the validator, which parses it to a dict.
    monkeypatch.setenv("HANDOFF_HEADER_MAP", '{"project": "X-AEP-Project"}')
    s = Settings()
    assert s.handoff_header_map == {"project": "X-AEP-Project"}
