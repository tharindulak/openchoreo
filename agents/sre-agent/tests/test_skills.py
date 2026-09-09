# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for skill loading — external-mount-first resolution.

The handoff skill 'coding-agent-handoff' is owned by AEP and delivered via a deploy-time
mount (EXTERNAL_SKILLS_DIR), so the loader must resolve an external directory
before the built-in library and fail clearly when a skill is missing.
"""

import tempfile
from pathlib import Path

from src.agent import skills as skills_mod
from src.agent.skills import discover_skills, load_skill, load_skills
from src.config import settings

_SKILL_MD = """---
name: {name}
description: {desc}
---

Body for {name}.
"""


def _write_skill(root: Path, name: str, desc: str = "desc") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(name=name, desc=desc))


def test_external_dir_takes_precedence():
    with tempfile.TemporaryDirectory() as ext, tempfile.TemporaryDirectory() as builtin:
        ext_p, builtin_p = Path(ext), Path(builtin)
        _write_skill(ext_p, "coding-agent-handoff", "from-external")
        _write_skill(builtin_p, "coding-agent-handoff", "from-builtin")
        skill = load_skill("coding-agent-handoff", [ext_p, builtin_p])
        assert skill.description == "from-external", skill.description


def test_fallback_to_builtin():
    with tempfile.TemporaryDirectory() as ext, tempfile.TemporaryDirectory() as builtin:
        ext_p, builtin_p = Path(ext), Path(builtin)
        _write_skill(builtin_p, "coding-agent-handoff", "from-builtin")  # only in built-in
        skill = load_skill("coding-agent-handoff", [ext_p, builtin_p])
        assert skill.description == "from-builtin", skill.description


def test_not_found_raises_clear_error():
    with tempfile.TemporaryDirectory() as ext:
        try:
            load_skill("coding-agent-handoff", [Path(ext)])
        except FileNotFoundError as exc:
            assert "coding-agent-handoff" in str(exc), exc
            assert "EXTERNAL_SKILLS_DIR" in str(exc), exc
        else:
            raise AssertionError("expected FileNotFoundError when skill is absent")


def test_name_mismatch_raises():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "coding-agent-handoff"
        d.mkdir()
        (d / "SKILL.md").write_text(_SKILL_MD.format(name="other", desc="x"))
        try:
            load_skill("coding-agent-handoff", [Path(root)])
        except ValueError as exc:
            assert "expected 'coding-agent-handoff'" in str(exc), exc
        else:
            raise AssertionError("expected ValueError on name mismatch")


def test_search_dirs_prefers_external_setting():
    original = settings.external_skills_dir
    try:
        settings.external_skills_dir = "/mnt/skills"
        dirs = skills_mod._search_dirs()
        assert dirs[0] == Path("/mnt/skills"), dirs
        assert dirs[-1] == skills_mod.SKILLS_DIR, dirs
    finally:
        settings.external_skills_dir = original


def test_search_dirs_builtin_only_when_unset():
    original = settings.external_skills_dir
    try:
        settings.external_skills_dir = ""
        dirs = skills_mod._search_dirs()
        assert dirs == [skills_mod.SKILLS_DIR], dirs
    finally:
        settings.external_skills_dir = original


def test_load_skills_sorted():
    with tempfile.TemporaryDirectory() as root:
        root_p = Path(root)
        _write_skill(root_p, "coding-agent-handoff")
        _write_skill(root_p, "aardvark")
        result = load_skills({"coding-agent-handoff", "aardvark"}, [root_p])
        assert [s.name for s in result] == ["aardvark", "coding-agent-handoff"], result


def test_discover_finds_every_mounted_skill():
    with tempfile.TemporaryDirectory() as root:
        root_p = Path(root)
        _write_skill(root_p, "coding-agent-handoff")
        _write_skill(root_p, "aardvark")
        result = discover_skills([root_p])
        assert [s.name for s in result] == ["aardvark", "coding-agent-handoff"], result


def test_discover_ignores_a_directory_with_no_skill_md():
    with tempfile.TemporaryDirectory() as root:
        root_p = Path(root)
        _write_skill(root_p, "coding-agent-handoff")
        (root_p / "diagrams").mkdir()  # a human-facing asset folder, not a skill
        (root_p / "diagrams" / "flow.png").write_bytes(b"not a skill")
        result = discover_skills([root_p])
        assert [s.name for s in result] == ["coding-agent-handoff"], result


def test_discover_skips_a_malformed_skill_without_failing_the_rest():
    # One contributor's broken SKILL.md must not disable everyone else's —
    # unlike load_skill/load_skills, which still raise for a single NAMED
    # skill a stage declares outright.
    with tempfile.TemporaryDirectory() as root:
        root_p = Path(root)
        _write_skill(root_p, "coding-agent-handoff")
        broken = root_p / "broken-skill"
        broken.mkdir()
        (broken / "SKILL.md").write_text("not frontmatter at all")
        result = discover_skills([root_p])
        assert [s.name for s in result] == ["coding-agent-handoff"], result


def test_discover_skips_a_name_mismatch_the_same_way():
    with tempfile.TemporaryDirectory() as root:
        root_p = Path(root)
        _write_skill(root_p, "coding-agent-handoff")
        mismatched = root_p / "mismatched"
        mismatched.mkdir()
        (mismatched / "SKILL.md").write_text(_SKILL_MD.format(name="other", desc="x"))
        result = discover_skills([root_p])
        assert [s.name for s in result] == ["coding-agent-handoff"], result


def test_discover_root_precedence_matches_load_skill():
    with tempfile.TemporaryDirectory() as ext, tempfile.TemporaryDirectory() as builtin:
        ext_p, builtin_p = Path(ext), Path(builtin)
        _write_skill(ext_p, "coding-agent-handoff", "from-external")
        _write_skill(builtin_p, "coding-agent-handoff", "from-builtin")
        _write_skill(builtin_p, "aardvark", "builtin-only")
        result = discover_skills([ext_p, builtin_p])
        by_name = {s.name: s for s in result}
        assert by_name["coding-agent-handoff"].description == "from-external"
        assert by_name["aardvark"].description == "builtin-only"


def test_discover_empty_directory_is_not_an_error():
    with tempfile.TemporaryDirectory() as root:
        assert discover_skills([Path(root)]) == []


def test_discover_missing_directory_is_not_an_error():
    assert discover_skills([Path("/does/not/exist")]) == []
