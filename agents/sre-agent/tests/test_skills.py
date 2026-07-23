# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for skill loading — external-mount-first resolution.

The handoff skill 'issue-fix' is owned by AEP and delivered via a deploy-time
mount (EXTERNAL_SKILLS_DIR), so the loader must resolve an external directory
before the built-in library and fail clearly when a skill is missing.

Runnable with pytest, or directly (`python tests/test_skills.py`) while the repo
has no pytest dependency wired up.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent import skills as skills_mod  # noqa: E402
from src.agent.skills import load_skill, load_skills  # noqa: E402
from src.config import settings  # noqa: E402

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
        _write_skill(ext_p, "issue-fix", "from-external")
        _write_skill(builtin_p, "issue-fix", "from-builtin")
        skill = load_skill("issue-fix", [ext_p, builtin_p])
        assert skill.description == "from-external", skill.description


def test_fallback_to_builtin():
    with tempfile.TemporaryDirectory() as ext, tempfile.TemporaryDirectory() as builtin:
        ext_p, builtin_p = Path(ext), Path(builtin)
        _write_skill(builtin_p, "issue-fix", "from-builtin")  # only in built-in
        skill = load_skill("issue-fix", [ext_p, builtin_p])
        assert skill.description == "from-builtin", skill.description


def test_not_found_raises_clear_error():
    with tempfile.TemporaryDirectory() as ext:
        try:
            load_skill("issue-fix", [Path(ext)])
        except FileNotFoundError as exc:
            assert "issue-fix" in str(exc), exc
            assert "EXTERNAL_SKILLS_DIR" in str(exc), exc
        else:
            raise AssertionError("expected FileNotFoundError when skill is absent")


def test_name_mismatch_raises():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "issue-fix"
        d.mkdir()
        (d / "SKILL.md").write_text(_SKILL_MD.format(name="other", desc="x"))
        try:
            load_skill("issue-fix", [Path(root)])
        except ValueError as exc:
            assert "expected 'issue-fix'" in str(exc), exc
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
        _write_skill(root_p, "issue-fix")
        _write_skill(root_p, "aardvark")
        result = load_skills({"issue-fix", "aardvark"}, [root_p])
        assert [s.name for s in result] == ["aardvark", "issue-fix"], result


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
    sys.exit(1 if failures else 0)
