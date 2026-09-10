# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware, TodoListMiddleware
from langchain.agents.structured_output import (
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from common.auth.bearer import BearerTokenAuth
from src.agent.fingerprint import error_fingerprint
from src.agent.handoff_headers import build_handoff_headers
from src.agent.middleware import (
    LogCaptureMiddleware,
    LoggingMiddleware,
    OutputTransformerMiddleware,
    ToolCallRecorder,
    ToolErrorHandlerMiddleware,
)
from src.agent.skills import Skill, create_load_skill_tool, discover_skills, load_skills
from src.agent.stream_parser import ChatResponseParser
from src.agent.tool_registry import (
    ALL_TOOL_FACTORIES,
    OBSERVABILITY_TOOLS,
    OPENCHOREO_TOOLS,
    TOOL_ACTIVE_FORMS,
    TOOLS,
)
from src.auth import get_oauth2_auth
from src.clients import MCPClient, get_model, get_report_backend, resolve_api_key
from src.clients.sink import get_report_sink, should_publish_report
from src.config import settings
from src.helpers import AlertScope
from src.logging_config import request_id_context
from src.models import ChatResponse, HandoffResult, RCAReport
from src.models.rca_report import RootCauseIdentified
from src.models.remediation_result import RemediationResult
from src.templates import render

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        *,
        template: str,
        tools: set[str] | Callable[[], set[str]],
        middleware: list[type],
        response_format: type[BaseModel] | None,
        recursion_limit: int,
        use_summarization: bool = False,
        tool_factories: list[Callable[..., BaseTool]] | None = None,
        skills: set[str] | Callable[[], list[Skill]] | None = None,
        tools_from_server: str | None = None,
    ):
        self.template = template
        self.tools = tools
        self.response_format = response_format
        self.recursion_limit = recursion_limit
        self._middleware_classes = middleware
        self._use_summarization = use_summarization
        self._tool_factories = tool_factories or []
        self._skills = skills or set()
        self._tools_from_server = tools_from_server

    async def create(
        self,
        auth: httpx.Auth,
        usage_callback: BaseCallbackHandler | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Runnable, LoggingMiddleware | None]:
        # Resolved fresh on every call (not cached on self) so a key rotated
        # via the AE console — synced by ESO into RCA_LLM_API_KEY_FILE — takes
        # effect on the next analysis/chat request without a pod restart.
        model = get_model(model_name=settings.rca_model_name, api_key=resolve_api_key())
        tools: list[BaseTool] = []
        # Names of tools discovered from the MCP connection — as opposed to
        # local tools (`self._tool_factories`, `create_load_skill_tool`'s
        # output) added below. Generic: which tools came from the connection
        # this Agent was told to discover from, not any particular tool's
        # name. Fed to ToolCallRecorder so it only records calls that
        # actually crossed to that connection — see its own docstring.
        connection_tool_names: set[str] = set()

        handoff_headers = context.get("handoff_headers") if context else None
        if self._tools_from_server:
            # Standard MCP discovery: every tool this named connection
            # advertises, unfiltered. No tool name is ever spelled here — the
            # handoff connection's own tool list IS the allow-list.
            mcp_client = MCPClient(auth=auth, handoff_headers=handoff_headers)
            tools = list(await mcp_client.get_tools(server_name=self._tools_from_server))
            connection_tool_names = {t.name for t in tools}
            logger.debug(
                "Discovered %d tools from '%s': %s",
                len(tools),
                self._tools_from_server,
                [t.name for t in tools],
            )
        else:
            wanted = self.tools() if callable(self.tools) else self.tools
            if wanted:
                mcp_client = MCPClient(auth=auth, handoff_headers=handoff_headers)
                all_tools = await mcp_client.get_tools()
                tools = [t for t in all_tools if t.name in wanted]
                connection_tool_names = {t.name for t in tools}
                logger.debug("Filtered to %d MCP tools: %s", len(tools), [t.name for t in tools])

        for factory in self._tool_factories:
            tools.append(factory(auth))

        # A callable discovers by directory (whatever's mounted, resolved
        # fresh); a literal set names skills up front and raises if one is
        # missing. Both produce the same shape, a catalog of Skill objects.
        if callable(self._skills):
            skills_catalog = self._skills()
        elif self._skills:
            skills_catalog = load_skills(self._skills)
        else:
            skills_catalog = []
        if skills_catalog:
            tools.append(create_load_skill_tool(skills_catalog))

        logger.debug("Total tools: %d — %s", len(tools), [t.name for t in tools])

        template_context = {
            "tools": tools,
            "observability_tools": [t for t in tools if t.name in OBSERVABILITY_TOOLS],
            "openchoreo_tools": [t for t in tools if t.name in OPENCHOREO_TOOLS],
            "skills_catalog": skills_catalog,
        }
        if context:
            template_context.update(context)

        middleware = [m() for m in self._middleware_classes]
        # An instance, not a class: it holds this run's own list, appended to
        # as the run makes tool calls.
        if context is not None:
            tool_call_log = context.get("tool_call_log")
            if tool_call_log is not None:
                middleware.append(ToolCallRecorder(tool_call_log, connection_tool_names))
        # Only the caller that asks for it gets logs captured — today that is
        # run_analysis's RCA_AGENT call. CHAT_AGENT uses the same tool for an
        # unrelated, ad-hoc conversation, and must not feed a fingerprint.
        log_capture = context.get("log_capture") if context else None
        if log_capture is not None:
            middleware.append(LogCaptureMiddleware(log_capture))
        if self._use_summarization:
            middleware.append(SummarizationMiddleware(model=model, trigger=("fraction", 0.8)))

        logging_mw = next((m for m in middleware if isinstance(m, LoggingMiddleware)), None)

        # None means no structured output at all — the turn ends on the
        # model's own final message rather than a synthetic response-schema
        # tool call. Anthropic and Gemini reject native strict structured
        # output combined with many tools (grammar-too-large / unsupported
        # response_mime_type), so use tool-based structured output for them
        # when there IS a schema; OpenAI keeps ProviderStrategy.
        output_strategy = None
        if self.response_format is not None:
            provider = settings.rca_model_name.split(":", 1)[0]
            if provider in ("anthropic",):
                output_strategy = ToolStrategy(self.response_format)
            else:
                output_strategy = ProviderStrategy(self.response_format)

        agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=render(self.template, template_context),
            middleware=middleware,
            response_format=output_strategy,
        )

        runnable_config: RunnableConfig = {"recursion_limit": self.recursion_limit}
        if usage_callback is not None:
            runnable_config["callbacks"] = [usage_callback]

        logger.info("Created agent with %d tools: %s", len(tools), [t.name for t in tools])
        return agent.with_config(runnable_config), logging_mw


RCA_AGENT = Agent(
    template="prompts/rca_agent_prompt.j2",
    tools={
        TOOLS.QUERY_COMPONENT_LOGS,
        TOOLS.QUERY_RESOURCE_METRICS,
        TOOLS.QUERY_TRACES,
        TOOLS.QUERY_TRACE_SPANS,
        TOOLS.LIST_COMPONENTS,
        TOOLS.GET_COMPONENT_RELEASE,
    },
    middleware=[
        LoggingMiddleware,
        ToolErrorHandlerMiddleware,
        OutputTransformerMiddleware,
        TodoListMiddleware,
    ],
    response_format=RCAReport,
    recursion_limit=200,
    use_summarization=True,
)

REMED_AGENT = Agent(
    template="prompts/remed_agent_prompt.j2",
    tools=set(),
    tool_factories=ALL_TOOL_FACTORIES,
    middleware=[
        LoggingMiddleware,
        ToolErrorHandlerMiddleware,
    ],
    response_format=RemediationResult,
    recursion_limit=50,
)


def handoff_skills() -> list[Skill]:
    """The handoff stage's skill catalog: whatever is mounted, discovered
    fresh per request. Empty is fatal, matching the single-named-skill case
    this replaced — the stage has no playbook without at least one."""
    skills = discover_skills()
    if not skills:
        raise FileNotFoundError(
            "No skill found under EXTERNAL_SKILLS_DIR "
            f"({settings.external_skills_dir!r}). The handoff stage's whole "
            "playbook is a mounted skill; see deploy-time skill mounting docs."
        )
    return skills


HANDOFF_AGENT = Agent(
    template="prompts/handoff_agent_prompt.j2",
    tools=set(),
    # Standard MCP discovery: every tool the "handoff" connection advertises.
    # No provider descriptor names them — see MCPClient.get_tools(server_name=...).
    tools_from_server="handoff",
    middleware=[
        LoggingMiddleware,
        ToolErrorHandlerMiddleware,
    ],
    # No structured output: the skill's only job is composing the issue via
    # whichever create-issue-shaped tool the connection advertises.
    response_format=None,
    recursion_limit=50,
    # Discovered by directory, not named: whatever is mounted under
    # EXTERNAL_SKILLS_DIR is in the catalog, so a second skill needs no code
    # change here — only a ConfigMap + volume. Fatal on zero found: this
    # stage's entire playbook IS a mounted skill.
    skills=handoff_skills,
)

CHAT_AGENT = Agent(
    template="prompts/chat_agent_prompt.j2",
    tools={
        TOOLS.QUERY_COMPONENT_LOGS,
        TOOLS.QUERY_RESOURCE_METRICS,
        TOOLS.QUERY_TRACES,
        TOOLS.QUERY_TRACE_SPANS,
        TOOLS.LIST_COMPONENTS,
    },
    middleware=[
        LoggingMiddleware,
        ToolErrorHandlerMiddleware,
        OutputTransformerMiddleware,
    ],
    response_format=ChatResponse,
    recursion_limit=50,
    use_summarization=True,
)


# Module-level semaphore for limiting concurrent analyses
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent_analyses)
    return _semaphore


async def stream_chat(
    messages: list[dict[str, str]],
    token: str,
    report_context: dict[str, Any] | None = None,
    scope: AlertScope | None = None,
) -> AsyncIterator[str]:
    request_id_context.set(f"msg_{uuid.uuid4().hex[:12]}")

    def emit(event: dict[str, Any]) -> str:
        return json.dumps(event) + "\n"  # Newline for ndjson

    try:
        agent, chat_logging = await CHAT_AGENT.create(
            auth=BearerTokenAuth(token),
            context={"scope": scope, "report_context": report_context},
        )

        agent_messages = list(messages)

        parser = ChatResponseParser()

        try:
            async for chunk, _ in agent.astream(
                {"messages": agent_messages},
                stream_mode="messages",
            ):
                # Skip non-AI message chunks (e.g., ToolMessage has content as list)
                if not isinstance(chunk.content, str):
                    continue

                for block in chunk.content_blocks:
                    block_type = block.get("type")

                    if block_type == "tool_call_chunk":
                        tool_name = block.get("name")
                        args = block.get("args", "")
                        if tool_name:
                            active_form = TOOL_ACTIVE_FORMS.get(tool_name)
                            yield emit(
                                {
                                    "type": "tool_call",
                                    "tool": tool_name,
                                    "activeForm": active_form,
                                    "args": args,
                                }
                            )

                    elif block_type == "text":
                        text = block.get("text", "")
                        if text:
                            delta = parser.push(text)
                            if delta:
                                yield emit({"type": "message_chunk", "content": delta})
        except StructuredOutputValidationError:
            logger.warning("Structured output validation failed, using streamed content")

        if chat_logging and (summary := chat_logging.tool_call_summary()):
            logger.debug("Chat tool calls: %s", summary)

        # Emit actions event if actions exist
        if parser.actions:
            yield emit({"type": "actions", "actions": parser.actions})

        # Build done event with parsed response
        yield emit({"type": "done", "message": parser.message})

    except Exception as e:
        logger.error("Chat stream error: %s", e, exc_info=True)
        yield emit(
            {
                "type": "error",
                "message": f"An error occured (request_id: {request_id_context.get()})",
            }
        )


async def run_analysis(
    report_id: str,
    alert_id: str,
    alert: Any,
    scope: AlertScope,
    meta: dict[str, Any] | None = None,
) -> None:
    # Set request_id in context for logging (use report_id as it's unique per request)
    request_id_context.set(report_id)

    semaphore = _get_semaphore()
    report_backend = get_report_backend()

    logger.info("Analysis task queued")

    async with semaphore:
        logger.info("Analysis task started")

        try:
            usage_callback = UsageMetadataCallbackHandler()

            # Populated by LogCaptureMiddleware as the RCA stage queries logs —
            # code-observed, read back after the run so the dedupe fingerprint
            # is built from what the observability plane actually returned,
            # not from whichever lines the model chose to cite afterward.
            raw_log_lines: list[dict[str, Any]] = []
            rca_agent, rca_logging = await RCA_AGENT.create(
                auth=get_oauth2_auth(),
                usage_callback=usage_callback,
                context={"log_capture": raw_log_lines},
            )

            content = render(
                "api/rca_request.j2",
                {"alert": alert, "meta": meta, "scope": scope},
            )

            rca_result = await asyncio.wait_for(
                rca_agent.ainvoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": content,
                            }
                        ],
                    }
                ),
                timeout=settings.analysis_timeout_seconds,
            )

            rca_report: RCAReport = rca_result["structured_response"]
            if rca_logging and (summary := rca_logging.tool_call_summary()):
                logger.debug("RCA tool calls: %s", summary)
            logger.info("RCA completed: usage=%s", usage_callback.usage_metadata)

            report_data = rca_report.model_dump()

            if settings.remed_agent and isinstance(rca_report.result, RootCauseIdentified):
                try:
                    logger.info("Running remediation agent")
                    remed_agent, remed_logging = await REMED_AGENT.create(
                        auth=get_oauth2_auth(),
                        usage_callback=usage_callback,
                        context={"scope": scope},
                    )

                    remed_result = await asyncio.wait_for(
                        remed_agent.ainvoke(
                            {
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": rca_report.model_dump_json(
                                            exclude={
                                                "result": {
                                                    "recommendations": {
                                                        "observability_recommendations"
                                                    }
                                                }
                                            }
                                        ),
                                    }
                                ],
                            }
                        ),
                        timeout=settings.analysis_timeout_seconds,
                    )

                    remed_report: RemediationResult = remed_result["structured_response"]
                    if remed_logging and (summary := remed_logging.tool_call_summary()):
                        logger.debug("Remediation tool calls: %s", summary)
                    report_data["result"]["recommendations"]["recommended_actions"] = [
                        a.model_dump() for a in remed_report.recommended_actions
                    ]
                    logger.info("Remediation completed: usage=%s", usage_callback.usage_metadata)
                except Exception as e:
                    logger.error("Remediation agent failed, saving RCA report without it: %s", e)

            if settings.handoff_enabled and isinstance(rca_report.result, RootCauseIdentified):
                # The statuses themselves, not a verdict over them. What they
                # MEAN is the receiver's own contract — it reads them back and
                # answers with whatever classification it chose. One entry per
                # action, None where remediation set none: filtering the Nones
                # out would read as an action-free report, which is the
                # opposite conclusion.
                action_statuses = [
                    action.get("status")
                    for action in report_data["result"]["recommendations"][
                        "recommended_actions"
                    ]
                ]
                try:
                    logger.info("Running handoff agent")
                    # Filled by the recorder with every tool call the stage
                    # made; the caller reads the LAST one — the skill's own
                    # constraint ("Creating that issue is your only write")
                    # guarantees that is the filing call, without this code
                    # needing to name it.
                    tool_calls: list[dict[str, Any]] = []
                    handoff_agent, handoff_logging = await HANDOFF_AGENT.create(
                        auth=get_oauth2_auth(),
                        usage_callback=usage_callback,
                        context={
                            "scope": scope,
                            "tool_call_log": tool_calls,
                            # Every deterministic, model-independent fact this
                            # run carries — incident identity and the action
                            # statuses alike — rendered under whatever header
                            # names deploy-time config maps them to. Out of the
                            # model's reach: it never sees these values as
                            # arguments it could restate.
                            "handoff_headers": build_handoff_headers(
                                {
                                    "project": scope.project,
                                    "component": scope.component,
                                    "signature": error_fingerprint(report_data, raw_log_lines),
                                    "action_statuses": action_statuses,
                                },
                                settings.handoff_header_map,
                            ),
                        },
                    )

                    # The full report: content-shaping (which sections matter,
                    # what to exclude) is the skill's own instructions now, not
                    # a Python filter — see coding-agent-handoff/SKILL.md.
                    messages: list[dict[str, Any]] = [
                        {"role": "user", "content": json.dumps(report_data)}
                    ]
                    await asyncio.wait_for(
                        handoff_agent.ainvoke({"messages": messages}),
                        timeout=settings.analysis_timeout_seconds,
                    )

                    outcome = tool_calls[-1] if tool_calls else None
                    handoff_report = HandoffResult.compose(outcome)
                    if handoff_logging and (summary_line := handoff_logging.tool_call_summary()):
                        logger.debug("Handoff tool calls: %s", summary_line)
                    report_data["handoff"] = handoff_report.model_dump()
                    logger.info(
                        "Handoff completed: tool=%s, result=%s",
                        handoff_report.tool,
                        handoff_report.result,
                    )
                except Exception as e:
                    # Recorded, not just logged: an absent `handoff` key is
                    # also what a legitimate "nothing to hand over" looks
                    # like, so a crash would read as a decision.
                    report_data["handoff"] = HandoffResult.failed(e).model_dump()
                    logger.error("Handoff agent failed, recorded on the RCA report: %s", e)

            response = await report_backend.upsert_rca_report(
                report_id=report_id,
                alert_id=alert_id,
                status="completed",
                report=report_data,
                environment_uid=scope.environment_uid,
                project_uid=scope.project_uid,
            )
            logger.info(
                "Updated RCA report to completed: index=%s, status=%s",
                response.get("_index"),
                response.get("result"),
            )

            # Hand the finished report to whatever downstream system is
            # configured. The report is already durable in report_backend by this
            # point, so publishing is best-effort by construction: a sink that is
            # down or misconfigured must never cost the analysis.
            if sink := get_report_sink():
                publish, skip_reason = should_publish_report(report_data)
                if not publish:
                    logger.info("Skipping report publish: %s", skip_reason)
                else:
                    try:
                        published_id = await sink.publish(report_data, get_oauth2_auth())
                        logger.info("Published report to sink: id=%s", published_id)
                    except Exception as e:
                        logger.error(
                            "Failed to publish report to sink (report stored locally): %s", e
                        )

        except asyncio.CancelledError:
            logger.warning("Analysis cancelled before completion")
            # Bounded best-effort: try to mark the report 'failed' so the
            # caller doesn't see it stuck in 'pending' forever, but DON'T
            # let a slow/hung backend block shutdown past
            # drain_background_tasks' cancel_wait. shield() keeps the
            # upsert running after we've received CancelledError;
            # wait_for() caps it so a wedged backend can't keep us alive.
            _SHUTDOWN_UPSERT_TIMEOUT = 5.0
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        report_backend.upsert_rca_report(
                            report_id=report_id,
                            alert_id=alert_id,
                            status="failed",
                            summary=f"Analysis cancelled during shutdown (report_id: {report_id})",
                            environment_uid=scope.environment_uid,
                            project_uid=scope.project_uid,
                        )
                    ),
                    timeout=_SHUTDOWN_UPSERT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Cancellation upsert exceeded %.1fs for report_id=%s; "
                    "report will remain in 'pending' state",
                    _SHUTDOWN_UPSERT_TIMEOUT,
                    report_id,
                )
            except Exception as update_error:
                logger.error("Failed to update status: %s", update_error, exc_info=True)
            raise

        except TimeoutError:
            logger.error(
                "Analysis timed out after %d seconds",
                settings.analysis_timeout_seconds,
            )
            try:
                await report_backend.upsert_rca_report(
                    report_id=report_id,
                    alert_id=alert_id,
                    status="failed",
                    summary=f"Analysis timed out (report_id: {report_id})",
                    environment_uid=scope.environment_uid,
                    project_uid=scope.project_uid,
                )
            except Exception as update_error:
                logger.error("Failed to update status: %s", update_error, exc_info=True)

        except Exception as e:
            logger.error("Analysis failed: error=%s", e, exc_info=True)
            try:
                await report_backend.upsert_rca_report(
                    report_id=report_id,
                    alert_id=alert_id,
                    status="failed",
                    summary=f"Analysis failed (report_id: {report_id})",
                    environment_uid=scope.environment_uid,
                    project_uid=scope.project_uid,
                )
            except Exception as update_error:
                logger.error("Failed to update status: %s", update_error, exc_info=True)
