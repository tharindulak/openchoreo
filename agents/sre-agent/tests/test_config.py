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
            handoff_provider_file="/etc/rca-agent/handoff/provider.json",
            external_skills_dir="",
        )


def test_handoff_accepts_a_complete_config():
    s = Settings(
        handoff_enabled=True,
        handoff_api_url="http://aep:3401",
        handoff_provider_file="/etc/rca-agent/handoff/provider.json",
        external_skills_dir="/etc/rca-agent/skills",
    )
    assert s.handoff_enabled
