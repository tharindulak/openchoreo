# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Minimal Skill loading for LangChain agents.

`create_agent()` only knows a flat `system_prompt` + `tools` — there's no
native progressive disclosure. This gives agents that shape anyway: skills
are `SKILL.md` files (frontmatter `name`/`description` + markdown body)
under `src/skills/<name>/SKILL.md`. An agent that declares `skills={...}`
gets a name+description catalog in its template context (cheap, always
loaded) and a `load_skill` tool that returns a skill's full body only when
the model decides it's relevant (loaded on demand, same as Claude Agent
SDK skills — just adapted to a framework with no built-in equivalent).
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


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


def load_skill(name: str) -> Skill:
    path = SKILLS_DIR / name / "SKILL.md"
    parsed_name, description, content = _parse_skill_md(path.read_text())
    if parsed_name != name:
        raise ValueError(f"{path} declares name={parsed_name!r}, expected {name!r}")
    return Skill(name=parsed_name, description=description, content=content)


def load_skills(names: set[str]) -> list[Skill]:
    return [load_skill(name) for name in sorted(names)]


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
