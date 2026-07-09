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

from src.agent.handoff_logic import (
    build_shortcut_result,
    classify_handoff_shortcut,
    wrap_ae_tools_for_handoff,
)
from src.agent.middleware import (
    LoggingMiddleware,
    OutputTransformerMiddleware,
    ToolErrorHandlerMiddleware,
)
from src.agent.skills import create_load_skill_tool, load_skills
from src.agent.stream_parser import ChatResponseParser
from src.agent.tool_registry import (
    AE_TOOLS,
    ALL_TOOL_FACTORIES,
    OBSERVABILITY_TOOLS,
    OPENCHOREO_TOOLS,
    TOOL_ACTIVE_FORMS,
    TOOLS,
)
from src.auth.bearer import BearerTokenAuth
from src.auth.oauth_client import get_oauth2_auth
from src.clients import MCPClient, get_model, get_report_backend, resolve_api_key
from src.config import settings
from src.helpers import AlertScope
from src.logging_config import request_id_context
from src.models import ChatResponse, HandoffResult, RCAReport
from src.models.rca_report import RootCauseIdentified
from src.models.remediation_result import RemediationResult
from src.template_manager import render

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        *,
        template: str,
        tools: set[str],
        middleware: list[type],
        response_format: type[BaseModel],
        recursion_limit: int,
        use_summarization: bool = False,
        tool_factories: list[Callable[..., BaseTool]] | None = None,
        skills: set[str] | None = None,
    ):
        self.template = template
        self.tools = tools
        self.response_format = response_format
        self.recursion_limit = recursion_limit
        self._middleware_classes = middleware
        self._use_summarization = use_summarization
        self._tool_factories = tool_factories or []
        self._skills = skills or set()

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

        if self.tools:
            mcp_client = MCPClient(auth=auth)
            all_tools = await mcp_client.get_tools()
            tools = [t for t in all_tools if t.name in self.tools]
            logger.debug("Filtered to %d MCP tools: %s", len(tools), [t.name for t in tools])

        for factory in self._tool_factories:
            tools.append(factory(auth))

        scope = context.get("scope") if context else None
        if scope is not None and TOOLS.AE_CREATE_ISSUE in {t.name for t in tools}:
            tools = wrap_ae_tools_for_handoff(tools, scope)

        skills_catalog = []
        if self._skills:
            skills_catalog = load_skills(self._skills)
            tools.append(create_load_skill_tool(skills_catalog))

        logger.debug("Total tools: %d — %s", len(tools), [t.name for t in tools])

        template_context = {
            "tools": tools,
            "observability_tools": [t for t in tools if t.name in OBSERVABILITY_TOOLS],
            "openchoreo_tools": [t for t in tools if t.name in OPENCHOREO_TOOLS],
            "ae_tools": [t for t in tools if t.name in AE_TOOLS],
            "skills_catalog": skills_catalog,
        }
        if context:
            template_context.update(context)

        middleware = [m() for m in self._middleware_classes]
        if self._use_summarization:
            middleware.append(SummarizationMiddleware(model=model, trigger=("fraction", 0.8)))

        logging_mw = next((m for m in middleware if isinstance(m, LoggingMiddleware)), None)

        # Anthropic and Gemini reject native strict structured output combined
        # with many tools (grammar-too-large / unsupported response_mime_type),
        # so use tool-based structured output for them. OpenAI keeps ProviderStrategy.
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

HANDOFF_AGENT = Agent(
    template="prompts/handoff_agent_prompt.j2",
    tools={
        TOOLS.AE_SEARCH_RELATED_ISSUES,
        TOOLS.AE_CREATE_ISSUE,
        TOOLS.AE_DISPATCH_CODING_AGENT,
    },
    middleware=[
        LoggingMiddleware,
        ToolErrorHandlerMiddleware,
    ],
    response_format=HandoffResult,
    recursion_limit=50,
    skills={"issue-fix"},
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

            rca_agent, rca_logging = await RCA_AGENT.create(
                auth=get_oauth2_auth(), usage_callback=usage_callback
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

            if settings.ae_handoff and isinstance(rca_report.result, RootCauseIdentified):
                recommended_actions = report_data["result"]["recommendations"][
                    "recommended_actions"
                ]
                shortcut_classification = classify_handoff_shortcut(recommended_actions)

                if shortcut_classification is not None:
                    # Provably no code-level work exists (empty actions, or every
                    # action already config-handled/applied/dismissed) — skip the
                    # LLM call entirely rather than pay for a run that can only
                    # ever conclude "nothing to hand off".
                    handoff_report = build_shortcut_result(
                        shortcut_classification, recommended_actions
                    )
                    report_data["handoff"] = handoff_report.model_dump()
                    logger.info(
                        "Handoff short-circuited: classification=%s", shortcut_classification
                    )
                else:
                    try:
                        logger.info("Running handoff agent")
                        handoff_agent, handoff_logging = await HANDOFF_AGENT.create(
                            auth=get_oauth2_auth(),
                            usage_callback=usage_callback,
                            context={"scope": scope, "auto_dispatch": settings.ae_auto_dispatch},
                        )

                        handoff_result_raw = await asyncio.wait_for(
                            handoff_agent.ainvoke(
                                {
                                    "messages": [
                                        {
                                            "role": "user",
                                            # report_data reflects the remediation-revised
                                            # actions (status/change) when the remediation
                                            # agent ran — that's the config-vs-code signal
                                            # the handoff agent classifies on.
                                            "content": json.dumps(report_data),
                                        }
                                    ],
                                }
                            ),
                            timeout=settings.analysis_timeout_seconds,
                        )

                        handoff_report = handoff_result_raw["structured_response"]
                        if handoff_logging and (summary := handoff_logging.tool_call_summary()):
                            logger.debug("Handoff tool calls: %s", summary)
                        report_data["handoff"] = handoff_report.model_dump()
                        logger.info(
                            "Handoff completed: classification=%s, issue=%s, dispatch=%s",
                            handoff_report.classification,
                            handoff_report.created_issue_url,
                            handoff_report.dispatch_run_name,
                        )
                    except Exception as e:
                        logger.error("Handoff agent failed, saving RCA report without it: %s", e)

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
