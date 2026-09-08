# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""The vocabulary of whatever system this agent hands code-level work to.

The handoff itself is generic: decide whether a root cause needs a code change,
find related issues, and file ONE issue. This incident's identity — a stable
dedupe key, the alerting project and component — no longer travels as arguments
the model fills in on that call: it rides the transport as per-run HTTP headers,
set by the calling process from the alert's own scope, out of the model's reach.

The NAMES still do not belong here. One receiver reads the dedupe key off
``X-Tracker-Signature`` and answers with ``adopted``; another calls them
something else entirely. Hardcoding one receiver's spelling is how this agent
ended up carrying a contract it does not own — a contract it cannot test, and
whose every change became a change to this repository. So the header names and
the answer names live in a descriptor the receiver ships, alongside the skill it
already mounts, and this module is the only place that reads it.

What deliberately does NOT live here: anything the agent decides. The
descriptor renames things; it never changes what identity is sent, what is
recorded, or what the handoff concludes. A receiver that needs different
BEHAVIOUR is not a descriptor change.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import settings

logger = logging.getLogger(__name__)


class HandoffProviderError(ValueError):
    """The descriptor is missing, unreadable, or incomplete.

    Raised at STARTUP rather than at the first handoff. A descriptor is config,
    and config fails quietly: a missing dedupe-key name would not crash anything,
    it would just file a duplicate issue for every recurrence of every incident,
    and nobody would find out until the duplicates were noticed by a human.
    """


@dataclass(frozen=True)
class HandoffProvider:
    """One receiver's names for the things the handoff pins and reads back."""

    # The two tools the handoff stage drives.
    create_issue_tool: str
    search_issues_tool: str

    # Header names for the incident this run is about. The values are facts about
    # the ALERT rather than judgments, which is why they travel on the transport
    # and not as tool arguments the model fills in — but what they are CALLED is
    # the receiver's, exactly as the argument names were.
    header_project: str
    header_component: str
    header_signature: str

    # Argument names for the values the CALLER'S PROCESS pins onto a call, as
    # distinct from what its model decided. Forced by middleware, never asked of
    # the model — see HandoffStatusesMiddleware.
    arg_action_statuses: str = "actionStatuses"

    # Answer names. Only these four carry meaning the agent acts on.
    answer_issue_number: str = "number"
    answer_issue_url: str = "url"
    answer_already_filed: str = "deduped"
    answer_classification: str = "classification"

    # Everything else the receiver answers, carried through verbatim onto the
    # report as `provider_facts` and never interpreted here. That is the point:
    # a fact this agent cannot check is a fact it must not paraphrase.
    answer_facts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tools(self) -> set[str]:
        return {self.create_issue_tool, self.search_issues_tool}

    def incident_headers(
        self, project: str | None, component: str | None, signature: str | None
    ) -> dict[str, str]:
        """This incident's identity under the receiver's own header names.

        An identity this agent does not have is OMITTED rather than sent blank: a
        blank header is a value the receiver has to validate and reject, while an
        absent one falls back cleanly to the caller's own arguments.
        """
        pairs = (
            (self.header_project, project),
            (self.header_component, component),
            (self.header_signature, signature),
        )
        return {name: value for name, value in pairs if value}


_REQUIRED_TOOLS = ("create_issue", "search_related")
_REQUIRED_HEADERS = ("project", "component", "signature")


def _require_str(section: dict[str, Any], key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HandoffProviderError(
            f"handoff provider descriptor: {where}.{key} must be a non-empty string, got {value!r}"
        )
    return value.strip()


def parse_provider(raw: dict[str, Any]) -> HandoffProvider:
    """Build a provider from a decoded descriptor, refusing anything partial.

    Pure and dict-shaped so it can be tested without a file. Every required name
    is checked here rather than defaulted, because a plausible default is exactly
    what would let a typo through: `dedupKey` silently unset means no dedupe key
    on the call, which files a fresh issue for every recurrence.
    """
    if not isinstance(raw, dict):
        raise HandoffProviderError(
            f"handoff provider descriptor must be an object, got {type(raw)}"
        )

    tools = raw.get("tools")
    if not isinstance(tools, dict):
        raise HandoffProviderError("handoff provider descriptor: 'tools' object is required")
    headers = raw.get("incident_headers")
    if not isinstance(headers, dict):
        raise HandoffProviderError(
            "handoff provider descriptor: 'incident_headers' object is required"
        )
    answers = raw.get("answer_fields") if isinstance(raw.get("answer_fields"), dict) else {}
    # Optional with defaults on purpose: a descriptor written before these
    # existed still parses, and the defaults are AEP's own names. Making them
    # required would turn a rolling upgrade into a startup crash on the old
    # ConfigMap.
    arguments = raw.get("call_arguments") if isinstance(raw.get("call_arguments"), dict) else {}

    for key in _REQUIRED_TOOLS:
        _require_str(tools, key, "tools")
    for key in _REQUIRED_HEADERS:
        _require_str(headers, key, "incident_headers")

    facts = answers.get("facts") or []
    if not isinstance(facts, list) or any(not isinstance(v, str) for v in facts):
        raise HandoffProviderError(
            "handoff provider descriptor: 'answer_fields.facts' must be a list of strings"
        )

    return HandoffProvider(
        create_issue_tool=_require_str(tools, "create_issue", "tools"),
        search_issues_tool=_require_str(tools, "search_related", "tools"),
        header_project=_require_str(headers, "project", "incident_headers"),
        header_component=_require_str(headers, "component", "incident_headers"),
        header_signature=_require_str(headers, "signature", "incident_headers"),
        answer_issue_number=answers.get("issue_number") or "number",
        answer_issue_url=answers.get("issue_url") or "url",
        answer_already_filed=answers.get("already_filed") or "deduped",
        answer_classification=answers.get("classification") or "classification",
        arg_action_statuses=arguments.get("action_statuses") or "actionStatuses",
        answer_facts=tuple(facts),
    )


_cached: HandoffProvider | None = None


def load_provider(path: str | None = None) -> HandoffProvider:
    """Read and validate the descriptor. Cached: it is deploy-time config.

    A handoff enabled without a readable descriptor is a hard failure, not a
    degraded mode — filing issues against the wrong argument names is worse than
    not filing at all, because the failure surfaces as duplicate or unadopted
    issues rather than as an error.
    """
    global _cached
    if path is None and _cached is not None:
        return _cached

    source = Path(path or settings.handoff_provider_file)
    try:
        raw = json.loads(source.read_text())
    except FileNotFoundError as e:
        raise HandoffProviderError(
            f"handoff provider descriptor not found at {source}. It is shipped by the receiving "
            "platform (mounted like the handoff skill); set HANDOFF_PROVIDER_FILE to its path."
        ) from e
    except json.JSONDecodeError as e:
        raise HandoffProviderError(
            f"handoff provider descriptor at {source} is not valid JSON: {e}"
        ) from e

    provider = parse_provider(raw)
    if path is None:
        _cached = provider
    logger.info(
        "Handoff provider loaded from %s: tools=%s headers=%s",
        source,
        sorted(provider.tools),
        [provider.header_project, provider.header_component, provider.header_signature],
    )
    return provider
