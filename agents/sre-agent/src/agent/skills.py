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
mount — e.g. the AEP-owned `coding-agent-handoff` skill delivered via a ConfigMap), then
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
        "For a deploy-time-mounted skill (e.g. 'coding-agent-handoff', owned by AEP), set "
        "EXTERNAL_SKILLS_DIR to the mounted skills directory."
    )


def load_skills(names: set[str], search_dirs: list[Path] | None = None) -> list[Skill]:
    roots = search_dirs if search_dirs is not None else _search_dirs()
    return [load_skill(name, roots) for name in sorted(names)]


def discover_skills(search_dirs: list[Path] | None = None) -> list[Skill]:
    """Every skill mounted under the skill roots, found by directory rather
    than named up front.

    Resolved fresh on every call, the same way tools are dynamically discovered
    from MCP servers at request time — a skill mounted alongside an existing
    one (a new ConfigMap + volume, no image rebuild) reaches the catalog on
    the agent's next request. A skill's CONTENT already worked this way
    (`load_skill` is a plain file read); this is the same treatment for which
    NAMES exist.

    A bad entry does not fail the whole catalog. `load_skill`/`load_skills`
    still raise for a single NAMED skill an agent declares outright — that
    stays loud, because that skill is the only one the stage has and a missing
    or malformed copy of it is a startup-shaped problem. Discovery is
    different: once a directory can hold more than one skill, one
    contributor's broken `SKILL.md` must not disable everyone else's. A
    directory with no `SKILL.md` is simply not a skill — a folder of assets
    for humans, say — and is skipped without comment; a directory that HAS one
    but fails to parse is loud (a warning naming the path and the error) and
    excluded rather than aborting discovery.

    Roots are walked in `load_skill`'s own precedence: a name already claimed
    by an earlier root is not reconsidered from a later one.
    """
    roots = search_dirs if search_dirs is not None else _search_dirs()
    found: dict[str, Skill] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in found:
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                name, description, content = _parse_skill_md(skill_file.read_text())
                if name != entry.name:
                    raise ValueError(
                        f"{skill_file} declares name={name!r}, expected {entry.name!r}"
                    )
            except (OSError, ValueError) as e:
                logger.warning("Skipping malformed skill at %s: %s", skill_file, e)
                continue
            found[entry.name] = Skill(name=name, description=description, content=content)
    return [found[name] for name in sorted(found)]


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
