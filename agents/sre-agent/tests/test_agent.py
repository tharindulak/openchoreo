# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent wiring (``Agent.create``) and orchestration
(``run_analysis``, ``stream_chat``). The LLM, MCP, and backend are mocked."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.agent.agent as agent_module
from src.agent.agent import Agent, RCA_AGENT, REMED_AGENT, run_analysis, stream_chat
from src.agent.middleware import LogCaptureMiddleware, LoggingMiddleware
from src.agent.tool_registry import TOOLS
from src.helpers import AlertScope
from src.models import RCAReport
from src.models.remediation_result import ActionStatus
from tests.factories import (
    make_rca_report,
    make_remediation_action,
    make_remediation_result,
)

AUTH = httpx.BasicAuth("user", "pass")
SCOPE = AlertScope(
    namespace="ns",
    project="p",
    project_uid="proj-uid",
    environment="dev",
    environment_uid="env-uid",
    component="c",
    component_uid="comp-uid",
)


def _tool(name):
    return SimpleNamespace(name=name)


def _make_agent(**overrides):
    kwargs = {
        "template": "prompts/x.j2",
        "tools": {"query_traces"},
        "middleware": [LoggingMiddleware],
        "response_format": RCAReport,
        "recursion_limit": 42,
    }
    kwargs.update(overrides)
    return Agent(**kwargs)


# ----------------------------------------------------------- Agent.create


@pytest.mark.asyncio
async def test_create_filters_mcp_tools():
    captured = {}
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    agent_obj = _make_agent(tools={"query_traces", "list_components"})

    with (
        patch("src.agent.agent.MCPClient") as mcp_cls,
        patch("src.agent.agent.create_agent", fake_create_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
    ):
        mcp_cls.return_value.get_tools = AsyncMock(
            return_value=[
                _tool("query_traces"),
                _tool("query_logs"),
                _tool("list_components"),
            ]
        )
        runnable, logging_mw = await agent_obj.create(auth=AUTH)

    names = [t.name for t in captured["tools"]]
    assert "query_traces" in names  # in the allowlist
    assert "query_logs" not in names  # filtered out
    assert "list_components" in names  # in the allowlist
    assert captured["system_prompt"] == "PROMPT"
    assert runnable == "CONFIGURED"
    assert isinstance(logging_mw, LoggingMiddleware)
    fake_agent.with_config.assert_called_once_with({"recursion_limit": 42})


def test_analysis_agents_use_native_binding_tools():
    expected = {
        TOOLS.LIST_RELEASE_BINDINGS,
        TOOLS.GET_RELEASE_BINDING,
        TOOLS.LIST_RESOURCE_RELEASE_BINDINGS,
        TOOLS.GET_RESOURCE_RELEASE_BINDING,
    }

    assert expected <= RCA_AGENT.tools
    assert expected <= REMED_AGENT.tools


def test_remediation_agent_only_uses_read_tools():
    assert REMED_AGENT.tools == {
        TOOLS.LIST_COMPONENTS,
        TOOLS.GET_COMPONENT,
        TOOLS.LIST_WORKLOADS,
        TOOLS.GET_WORKLOAD,
        TOOLS.LIST_RELEASE_BINDINGS,
        TOOLS.GET_RELEASE_BINDING,
        TOOLS.GET_COMPONENT_RELEASE,
        TOOLS.GET_COMPONENT_RELEASE_SCHEMA,
        TOOLS.LIST_RESOURCE_RELEASE_BINDINGS,
        TOOLS.GET_RESOURCE_RELEASE_BINDING,
    }


@pytest.mark.asyncio
async def test_create_attaches_usage_callback():
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"
    cb = object()
    agent_obj = _make_agent(tools=set())

    with (
        patch("src.agent.agent.create_agent", return_value=fake_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        await agent_obj.create(auth=AUTH, usage_callback=cb)

    mcp_cls.assert_not_called()  # no MCP fetch when tools is empty
    fake_agent.with_config.assert_called_once_with({"recursion_limit": 42, "callbacks": [cb]})


@pytest.mark.asyncio
async def test_create_with_tools_from_server_takes_every_tool_unfiltered():
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"
    agent_obj = Agent(
        template="prompts/x.j2",
        tools=set(),
        tools_from_server="handoff",
        middleware=[LoggingMiddleware],
        response_format=None,
        recursion_limit=42,
    )

    with (
        patch("src.agent.agent.create_agent", return_value=fake_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(
            return_value=[_tool("ae_search_related_issues"), _tool("ae_create_issue")]
        )
        await agent_obj.create(auth=AUTH)

    mcp_cls.return_value.get_tools.assert_awaited_once_with(server_name="handoff")


@pytest.mark.asyncio
async def test_create_appends_tool_call_recorder_when_context_asks_for_it():
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"
    agent_obj = _make_agent(tools=set())

    with (
        patch("src.agent.agent.create_agent", return_value=fake_agent) as create_agent_mock,
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH, context={"tool_call_log": []})

    from src.agent.middleware import ToolCallRecorder

    middleware_instances = create_agent_mock.call_args.kwargs["middleware"]
    assert any(isinstance(m, ToolCallRecorder) for m in middleware_instances)


# ------------------------------------------------------------ run_analysis


def _patched_run(rca_report, backend, *, remed_result=None, remed_enabled=False):
    fake_rca = MagicMock()
    fake_rca.ainvoke = AsyncMock(return_value={"structured_response": rca_report})
    fake_remed = MagicMock()
    fake_remed.ainvoke = AsyncMock(
        return_value={"structured_response": remed_result or make_remediation_result()}
    )
    return (
        patch.object(agent_module.RCA_AGENT, "create", AsyncMock(return_value=(fake_rca, None))),
        patch.object(
            agent_module.REMED_AGENT, "create", AsyncMock(return_value=(fake_remed, None))
        ),
        patch("src.agent.agent.get_report_backend", return_value=backend),
        patch("src.agent.agent.get_oauth2_auth", return_value=MagicMock()),
        patch("src.agent.agent.render", return_value="CONTENT"),
        patch.object(agent_module.settings, "remed_agent", remed_enabled),
    )


@pytest.mark.asyncio
async def test_run_analysis_completes_and_upserts():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    patches = _patched_run(make_rca_report(), backend)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    backend.upsert_rca_report.assert_awaited_once()
    kw = backend.upsert_rca_report.await_args.kwargs
    assert kw["status"] == "completed"
    assert kw["report_id"] == "r1"
    assert kw["project_uid"] == "proj-uid"
    assert kw["report"]["summary"]


@pytest.mark.asyncio
async def test_run_analysis_marks_failed_on_timeout():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock()
    fake_rca = MagicMock()
    fake_rca.ainvoke = AsyncMock(side_effect=TimeoutError())

    with (
        patch.object(agent_module.RCA_AGENT, "create", AsyncMock(return_value=(fake_rca, None))),
        patch("src.agent.agent.get_report_backend", return_value=backend),
        patch("src.agent.agent.get_oauth2_auth", return_value=MagicMock()),
        patch("src.agent.agent.render", return_value="CONTENT"),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={}, scope=SCOPE)

    kw = backend.upsert_rca_report.await_args.kwargs
    assert kw["status"] == "failed"
    assert "timed out" in kw["summary"]


@pytest.mark.asyncio
async def test_run_analysis_marks_failed_on_error():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock()
    fake_rca = MagicMock()
    fake_rca.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch.object(agent_module.RCA_AGENT, "create", AsyncMock(return_value=(fake_rca, None))),
        patch("src.agent.agent.get_report_backend", return_value=backend),
        patch("src.agent.agent.get_oauth2_auth", return_value=MagicMock()),
        patch("src.agent.agent.render", return_value="CONTENT"),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={}, scope=SCOPE)

    kw = backend.upsert_rca_report.await_args.kwargs
    assert kw["status"] == "failed"
    assert "failed" in kw["summary"].lower()


@pytest.mark.asyncio
async def test_run_analysis_enriches_with_remediation_when_enabled():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    remed = make_remediation_result()
    patches = _patched_run(make_rca_report(), backend, remed_result=remed, remed_enabled=True)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await run_analysis(report_id="r1", alert_id="a1", alert={}, scope=SCOPE)

    saved = backend.upsert_rca_report.await_args.kwargs["report"]
    actions = saved["result"]["recommendations"]["recommended_actions"]
    assert actions[0]["description"] == remed.recommended_actions[0].description


# ------------------------------------------------------------- stream_chat


@pytest.mark.asyncio
async def test_stream_chat_emits_error_on_failure():
    with patch.object(
        agent_module.CHAT_AGENT, "create", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        events = [
            json.loads(line)
            async for line in stream_chat(messages=[{"role": "user", "content": "hi"}], token="t")
        ]

    assert len(events) == 1
    assert events[0]["type"] == "error"


@pytest.mark.asyncio
async def test_stream_chat_streams_message_and_done():
    payload = '{"message": "Hello"}'

    class FakeChunk:
        content = payload
        content_blocks = [{"type": "text", "text": payload}]

    async def fake_astream(*args, **kwargs):
        yield (FakeChunk(), {})

    fake_agent = MagicMock()
    fake_agent.astream = fake_astream

    with patch.object(
        agent_module.CHAT_AGENT, "create", AsyncMock(return_value=(fake_agent, None))
    ):
        events = [
            json.loads(line)
            async for line in stream_chat(messages=[{"role": "user", "content": "hi"}], token="t")
        ]

    types = [e["type"] for e in events]
    assert "message_chunk" in types
    assert events[-1]["type"] == "done"
    assert events[-1]["message"] == "Hello"


@pytest.mark.asyncio
async def test_run_analysis_records_a_failed_handoff_on_the_report():
    # A stage that threw must not leave the report in the same shape as one
    # that decided nothing needed handing over — that is the ambiguity a
    # human hits while triaging, and it hides a broken skill mount.
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=True)
    remed = make_remediation_result(
        recommended_actions=[make_remediation_action(status=ActionStatus.SUGGESTED)]
    )
    patches = _patched_run(make_rca_report(), backend, remed_result=remed, remed_enabled=True)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(
            agent_module.HANDOFF_AGENT,
            "create",
            AsyncMock(side_effect=RuntimeError("Skill 'x' not found")),
        ),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    handoff = backend.upsert_rca_report.await_args.kwargs["report"]["handoff"]
    assert handoff["tool"] is None
    assert handoff["result"] is None
    assert "Skill 'x' not found" in handoff["failure_reason"]


@pytest.mark.asyncio
async def test_run_analysis_skips_handoff_call_when_cooldown_active():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=False)
    patches = _patched_run(make_rca_report(), backend)

    handoff_create = AsyncMock()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(agent_module.HANDOFF_AGENT, "create", handoff_create),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    handoff_create.assert_not_called()
    kw = backend.upsert_rca_report.await_args.kwargs
    saved = kw["report"]
    # RCAReport.handoff defaults to None and model_dump() always includes the
    # key, so a suppressed handoff is "handoff is None", not "key absent".
    assert saved["handoff"] is None
    # The whole feature's central non-goal: RCA and remediation always run in
    # full, unaffected by anything the cooldown gate does. A suppressed
    # handoff must not read as a discarded analysis.
    assert kw["status"] == "completed"
    assert saved["summary"] == "Service was OOM-killed due to an undersized memory limit."
    assert saved["result"] is not None


@pytest.mark.asyncio
async def test_run_analysis_fails_open_when_cooldown_check_raises():
    """The cooldown gate is an optimization, not a correctness requirement. A
    transient DB error (a locked SQLite file, a dropped connection, anything)
    raised out of try_acquire_handoff_slot must not propagate to the outer
    handler and discard the RCA/remediation work already completed — the
    check fails open and the handoff proceeds as if the slot were free."""
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(side_effect=RuntimeError("db unavailable"))
    patches = _patched_run(make_rca_report(), backend)

    fake_handoff = MagicMock()
    fake_handoff.ainvoke = AsyncMock(return_value={"messages": []})
    handoff_create = AsyncMock(return_value=(fake_handoff, None))

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(agent_module.HANDOFF_AGENT, "create", handoff_create),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    # Fail-open means proceed as if the slot were free: the handoff agent is
    # still invoked.
    handoff_create.assert_awaited_once()
    kw = backend.upsert_rca_report.await_args.kwargs
    assert kw["status"] == "completed"
    assert kw["report"]["summary"] == "Service was OOM-killed due to an undersized memory limit."
    assert kw["report"]["result"] is not None


@pytest.mark.asyncio
async def test_run_analysis_skips_sink_publish_when_cooldown_suppresses_handoff():
    """A cooldown-suppressed occurrence is the same situation
    ``should_publish_report``'s own `deduped` check exists to handle: an
    earlier, very recent run already went through the full handoff decision
    for this same incident. It must not publish either, even though
    report_data["handoff"] is plain None here (not a dict with
    deduped=True) — which is exactly the shape should_publish_report would
    otherwise wave through."""
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=False)
    patches = _patched_run(make_rca_report(), backend)

    sink = MagicMock()
    sink.publish = AsyncMock(return_value="row-1")

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch("src.agent.agent.get_report_sink", return_value=sink),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    sink.publish.assert_not_called()


@pytest.mark.asyncio
async def test_run_analysis_calls_handoff_when_cooldown_slot_is_free():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=True)
    patches = _patched_run(make_rca_report(), backend)

    handoff_create = AsyncMock(side_effect=RuntimeError("no skill"))
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(agent_module.HANDOFF_AGENT, "create", handoff_create),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    handoff_create.assert_awaited_once()
    saved = backend.upsert_rca_report.await_args.kwargs["report"]
    # The key is always present (RCAReport.handoff defaults to None); what
    # proves the handoff actually ran is that it's no longer None.
    assert saved["handoff"] is not None


@pytest.mark.asyncio
async def test_run_analysis_dedupe_key_is_project_component_fingerprint():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=False)
    patches = _patched_run(make_rca_report(), backend)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(agent_module.settings, "handoff_cooldown_seconds", 900),
        patch.object(agent_module, "error_fingerprint", return_value="deadbeef01"),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    backend.try_acquire_handoff_slot.assert_awaited_once_with("p/c/deadbeef01", 900)


@pytest.mark.asyncio
async def test_run_analysis_dedupe_key_falls_back_to_nofp_with_no_fingerprint():
    backend = MagicMock()
    backend.upsert_rca_report = AsyncMock(return_value={"result": "created"})
    backend.try_acquire_handoff_slot = AsyncMock(return_value=False)
    patches = _patched_run(make_rca_report(), backend)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(agent_module.settings, "handoff_enabled", True),
        patch.object(agent_module, "error_fingerprint", return_value=None),
    ):
        await run_analysis(report_id="r1", alert_id="a1", alert={"x": 1}, scope=SCOPE)

    key = backend.try_acquire_handoff_slot.await_args.args[0]
    assert key == "p/c/nofp"


@pytest.mark.asyncio
async def test_create_resolves_a_callable_skills_catalog_fresh_per_call():
    # Discovery, not a literal set: the catalog comes from calling the
    # function, mirroring how `tools_from_server` already resolves the tool
    # list per request rather than at construction time.
    captured = {}
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    from src.agent.skills import Skill

    provided = [Skill(name="coding-agent-handoff", description="d", content="body")]
    agent_obj = _make_agent(tools=set(), skills=lambda: provided)

    with (
        patch("src.agent.agent.create_agent", fake_create_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH)

    tool_names = [t.name for t in captured["tools"]]
    assert "load_skill" in tool_names


@pytest.mark.asyncio
async def test_create_appends_no_load_skill_tool_when_the_catalog_is_empty():
    captured = {}
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    agent_obj = _make_agent(tools=set(), skills=lambda: [])

    with (
        patch("src.agent.agent.create_agent", fake_create_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH)

    assert "load_skill" not in [t.name for t in captured["tools"]]


@pytest.mark.asyncio
async def test_create_still_accepts_a_literal_skill_name_set():
    # The set[str] path (load_skills by name) stays available for an agent
    # that wants exactly one named skill, not directory discovery.
    captured = {}
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    from src.agent.skills import Skill

    agent_obj = _make_agent(tools=set(), skills={"coding-agent-handoff"})

    with (
        patch("src.agent.agent.create_agent", fake_create_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
        patch(
            "src.agent.agent.load_skills",
            return_value=[Skill(name="coding-agent-handoff", description="d", content="c")],
        ) as load_skills_mock,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH)

    load_skills_mock.assert_called_once_with({"coding-agent-handoff"})
    assert "load_skill" in [t.name for t in captured["tools"]]


def test_handoff_skills_raises_on_an_empty_catalog():
    # The stage's whole playbook is a mounted skill; an empty or unreadable
    # mount is a startup-shaped problem, not a quiet handoff with no skill.
    with (
        patch("src.agent.agent.discover_skills", return_value=[]),
        pytest.raises(FileNotFoundError, match="EXTERNAL_SKILLS_DIR"),
    ):
        agent_module.handoff_skills()


def test_handoff_skills_returns_whatever_was_discovered():
    from src.agent.skills import Skill

    found = [Skill(name="coding-agent-handoff", description="d", content="c")]
    with patch("src.agent.agent.discover_skills", return_value=found):
        assert agent_module.handoff_skills() == found


@pytest.mark.asyncio
async def test_create_passes_response_format_none_through_with_no_strategy_wrapper():
    # HANDOFF_AGENT runs with no structured output at all: the turn ends on
    # the model's own final message, not a synthetic response-schema tool
    # call. ToolStrategy/ProviderStrategy both require a real schema, so this
    # path must skip constructing either rather than wrapping None in one.
    captured = {}
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    agent_obj = _make_agent(tools=set(), response_format=None)

    with (
        patch("src.agent.agent.create_agent", fake_create_agent),
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
        patch("src.agent.agent.ToolStrategy") as tool_strategy_cls,
        patch("src.agent.agent.ProviderStrategy") as provider_strategy_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH)

    assert captured["response_format"] is None
    tool_strategy_cls.assert_not_called()
    provider_strategy_cls.assert_not_called()


@pytest.mark.asyncio
async def test_create_appends_log_capture_middleware_when_context_asks_for_it():
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"
    captured: list[dict] = []
    agent_obj = _make_agent(tools=set())

    with (
        patch("src.agent.agent.create_agent", return_value=fake_agent) as create_agent_mock,
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH, context={"log_capture": captured})

    middleware_instances = create_agent_mock.call_args.kwargs["middleware"]
    assert any(isinstance(m, LogCaptureMiddleware) for m in middleware_instances)


@pytest.mark.asyncio
async def test_create_appends_no_log_capture_middleware_when_context_omits_it():
    fake_agent = MagicMock()
    fake_agent.with_config.return_value = "CONFIGURED"
    agent_obj = _make_agent(tools=set())

    with (
        patch("src.agent.agent.create_agent", return_value=fake_agent) as create_agent_mock,
        patch("src.agent.agent.render", lambda *a, **k: "PROMPT"),
        patch("src.agent.agent.MCPClient") as mcp_cls,
    ):
        mcp_cls.return_value.get_tools = AsyncMock(return_value=[])
        await agent_obj.create(auth=AUTH)

    middleware_instances = create_agent_mock.call_args.kwargs["middleware"]
    assert not any(isinstance(m, LogCaptureMiddleware) for m in middleware_instances)
