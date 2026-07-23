# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Minimal Skill loading for LangChain agents.

`create_agent()` only knows a flat `system_prompt` + `tools` — there's no
native progressive disclosure. This gives agents that shape anyway: skills
are `SKILL.md` files (frontmatter `name`/`description` + markdown body)
under `<skills-dir>/<name>/SKILL.md`. An agent that declares `skills={...}`
gets a name+description catalog in its template context (cheap, always
loaded) and a `load_skill` tool that returns a skill's full body only when
the model decides it's relevant (loaded on demand, same as Claude Agent
SDK skills — just adapted to a framework with no built-in equivalent).

Skills are resolved from `settings.external_skills_dir` FIRST (a deploy-time
mount — e.g. the AEP-owned `issue-fix` skill delivered via a ConfigMap), then
from the built-in `src/skills` library baked into the image. The external
directory therefore overrides or adds to the built-in one.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from src.config import settings

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _search_dirs() -> list[Path]:
    """Skill roots in precedence order: the deploy-time external mount (if
    configured) first, then the built-in library."""
    dirs: list[Path] = []
    external = settings.external_skills_dir.strip()
    if external:
        dirs.append(Path(external))
    dirs.append(SKILLS_DIR)
    return dirs


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str


def _parse_skill_md(text: str) -> tuple[str, str, str]:
    """Split a SKILL.md file into (name, description, body)."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise ValueError("SKILL.md must start with a '---' frontmatter block")
    _, frontmatter, body = stripped.split("---", 2)

    name = description = ""
    for line in frontmatter.strip().splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "name":
            name = value
        elif key == "description":
            description = value

    if not name or not description:
        raise ValueError("SKILL.md frontmatter must set both 'name' and 'description'")
    return name, description, body.strip()


def load_skill(name: str, search_dirs: list[Path] | None = None) -> Skill:
    roots = search_dirs if search_dirs is not None else _search_dirs()
    for root in roots:
        path = root / name / "SKILL.md"
        if not path.is_file():
            continue
        parsed_name, description, content = _parse_skill_md(path.read_text())
        if parsed_name != name:
            raise ValueError(f"{path} declares name={parsed_name!r}, expected {name!r}")
        return Skill(name=parsed_name, description=description, content=content)

    searched = ", ".join(str(root / name / "SKILL.md") for root in roots)
    raise FileNotFoundError(
        f"Skill '{name}' not found. Searched: {searched}. "
        "For a deploy-time-mounted skill (e.g. 'issue-fix', owned by AEP), set "
        "EXTERNAL_SKILLS_DIR to the mounted skills directory."
    )


def load_skills(names: set[str], search_dirs: list[Path] | None = None) -> list[Skill]:
    roots = search_dirs if search_dirs is not None else _search_dirs()
    return [load_skill(name, roots) for name in sorted(names)]


class _LoadSkillInput(BaseModel):
    name: str = Field(..., description="Name of the skill to load, from the catalog above")


def create_load_skill_tool(skills: list[Skill]) -> BaseTool:
    by_name = {s.name: s for s in skills}

    async def _run(name: str) -> str:
        skill = by_name.get(name)
        if skill is None:
            available = ", ".join(sorted(by_name)) or "(none)"
            return f"Unknown skill '{name}'. Available skills: {available}"
        return skill.content

    return StructuredTool.from_function(
        coroutine=_run,
        name="load_skill",
        description=(
            "Load the full instructions for a named skill from the catalog in your "
            "system prompt. Call this before acting on whichever skill's description "
            "matches your task."
        ),
        args_schema=_LoadSkillInput,
    )
