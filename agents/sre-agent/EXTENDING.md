# Extending the SRE (RCA) Agent

The SRE agent has no formal plugin system, but it's built with clean, well-factored
**extension points**. This guide lists where to customize, grouped by how much code is
required, with the exact files involved.

## Quick reference — start here by goal

| You want to… | Extend here | Code? |
|---|---|---|
| Swap the LLM / provider | `RCA_MODEL_NAME` env (`src/config.py` → `src/clients/llm.py`) | no |
| Store reports elsewhere (S3, OpenSearch…) | implement `ReportBackend` (`src/clients/backend/`) | yes |
| Change *how* it reasons / what it recommends | prompts (`src/templates/prompts/*.j2`) | no (template) |
| Reshape telemetry before the LLM sees it | middleware templates (`src/templates/middleware/*.j2`) | no (template) |
| Give it new data sources / actions | tools (`src/agent/tool_registry.py`) | yes |
| Pre/post-process tool output, add guardrails | middleware (`AgentMiddleware` subclass) | yes |
| Add a new agent stage (like remediation) | `Agent(...)` in `src/agent/agent.py` + wire into `run_analysis` | yes |
| Change the report shape / contract | `src/models/rca_report.py` | yes |
| Toggle the remediation agent | `REMED_AGENT` env | no |
| Toggle the handoff stage | `HANDOFF_ENABLED` env, with `HANDOFF_API_URL`, `HANDOFF_MCP_PATH`, `HANDOFF_HEADER_MAP` and `EXTERNAL_SKILLS_DIR` (`src/config.py`) | no |
| Expose new capabilities to upstream agents | `@mcp_server.tool()` in `src/mcp_server.py` | yes |

---

## 1. Configuration-only (no code)

Env-driven settings in `src/config.py` (loaded from `.env` / container env):

| Setting | Env var | Purpose |
|---|---|---|
| Model / provider | `RCA_MODEL_NAME` (e.g. `anthropic:claude-sonnet-4-6`) | provider-agnostic via `init_chat_model` |
| LLM key | `RCA_LLM_API_KEY` | |
| Report backend | `REPORT_BACKEND` (`sqlite`/`postgresql`), `SQL_BACKEND_URI` | storage selection |
| Report sink | `REPORT_SINK` (`webhook`, or empty), `REPORT_SINK_URL` | publish finished reports downstream (§7a); empty = nowhere |
| Remediation agent | `REMED_AGENT` (`true`/`false`) | enable the 2nd (revise-recommendations) agent |
| Concurrency / timeout | `max_concurrent_analyses`, `analysis_timeout_seconds` | |
| Data sources | `OBSERVER_API_URL`, `OPENCHOREO_API_URL` | MCP / API endpoints |
| Handoff stage | `HANDOFF_ENABLED`, `HANDOFF_API_URL`, `HANDOFF_MCP_PATH`, `HANDOFF_HEADER_MAP`, `EXTERNAL_SKILLS_DIR` | hand code-level work to a receiving platform: its MCP endpoint, the per-run identity headers `HANDOFF_HEADER_MAP` maps context fields onto, and the skills mount its playbook arrives in (§8) |
| Auth | OAuth2 / JWT vars + `auth-config.yaml` | authn/authz |

## 2. Prompt customization (Jinja templates, no logic)

- **Agent behavior** — `src/templates/prompts/{rca,remed,chat}_agent_prompt.j2`.
  Rewrite instructions, investigation strategy, recommendation rules, output guidance.
- **Telemetry formatting** — `src/templates/middleware/{logs,metrics,traces,trace_spans}.j2`.
  Control how raw observability data is shaped before it reaches the LLM.
- **Not the handoff prompt** — `handoff_agent_prompt.j2` carries only the run-time scope
  values and the skill catalog; it is a loader. How the handoff decides, searches and
  writes its issue lives in the `coding-agent-handoff` skill the receiving platform
  mounts (`EXTERNAL_SKILLS_DIR`, see §1). Customize that skill, not this template.

Prompts are rendered with a context that includes the available tools (split into
`observability_tools` / `openchoreo_tools`) and the request scope — see
`Agent.create()` in `src/agent/agent.py`.

## 3. Tools — `src/agent/tool_registry.py`

Two mechanisms:

**a. MCP tool whitelist** (`TOOLS` class) — names of tools provided by the
Observability and OpenChoreo MCP servers, grouped by server. Add/remove which tools an
agent may call; the agent filters MCP tools to its declared set in `Agent.create()`.

**b. Local tool factories** (`ALL_TOOL_FACTORIES`) — in-process `StructuredTool`s that
call the OpenChoreo API directly with the caller's auth. Add a new one:

```python
class _MyToolInput(BaseModel):
    namespace: str = Field(..., description="Namespace name")

def create_my_tool(auth: httpx.Auth) -> StructuredTool:
    async def _run(namespace: str) -> str:
        return json.dumps(await get(f"/namespaces/{namespace}/something", auth))
    return StructuredTool.from_function(
        coroutine=_run, name="my_tool",
        description="What it does.", args_schema=_MyToolInput,
    )

ALL_TOOL_FACTORIES.append(create_my_tool)   # then add to an agent's tool_factories
```

## 4. Middleware — `src/agent/middleware/`

Subclass LangChain's `AgentMiddleware`. Shipped examples:
- `LoggingMiddleware` — request/tool-call logging
- `OutputTransformerMiddleware` — transforms tool results before the LLM (metric stats,
  anomaly detection, trace-hierarchy building, log grouping)
- `ToolErrorHandlerMiddleware` — graceful tool-error handling

Add your own (e.g. redaction, guardrails, custom output transforms) and include it in an
agent's `middleware` list.

## 5. Agents — `src/agent/agent.py`

The `Agent` factory is fully composable. Defining a new specialized agent is the same
pattern as `RCA_AGENT` / `REMED_AGENT` / `CHAT_AGENT`:

```python
MY_AGENT = Agent(
    template="prompts/my_agent_prompt.j2",
    tools={TOOLS.QUERY_COMPONENT_LOGS, TOOLS.QUERY_TRACES},
    tool_factories=[...],                 # optional local tools
    middleware=[LoggingMiddleware, ToolErrorHandlerMiddleware],
    response_format=MyResult,             # a Pydantic model
    recursion_limit=50,
    use_summarization=True,
)
```

Then invoke it where appropriate (e.g. add a stage in `run_analysis`, mirroring how the
remediation agent runs after the RCA agent).

> Provider note: `Agent.create()` picks `ToolStrategy` for Anthropic and `ProviderStrategy`
> otherwise, because Anthropic/Gemini reject native strict structured output with many
> tools (grammar-too-large / unsupported MIME type). Keep this in mind for new providers.

## 6. Structured output models — `src/models/`

`RCAReport`, `RemediationResult`, `ChatResponse` are the structured-output contracts
(forced via each agent's `response_format`). Change these to change what the agent must
produce — and remember downstream consumers (portal, your AE integration) parse this shape.

## 7. Report backend — `src/clients/backend/`

The cleanest "real" plugin seam: a `ReportBackend` **ABC** (`report_backend.py`) with
`@abstractmethod`s, plus a `get_report_backend()` factory. Implement a subclass for custom
storage (e.g. OpenSearch, S3) and wire it into the factory:

```python
class MyBackend(ReportBackend):
    async def upsert_rca_report(self, ...): ...
    async def get_rca_report(self, report_id): ...
    async def list_rca_reports(self, ...): ...
```

## 7a. Report sink — `src/clients/sink/`

The other direction, and the seam to use for a downstream integration. A backend is the
agent's own store and must answer reads; a **sink** is written to and never read back, so
`ReportSink` has one method and no lifecycle. `get_report_sink()` selects one by
`REPORT_SINK`; empty (the default) publishes nowhere.

```python
class MySink(ReportSink):
    async def publish(self, report: dict, auth: httpx.Auth) -> str | None: ...
```

The sink receives the report **as this agent models it**. It must not map field names,
spell a receiver's endpoint path, rename an enum or render a receiver's presentation
format — every one of those is a fact about the RECEIVER, and encoding them here means
carrying a contract this repo does not own and cannot test. The receiver maps what it is
sent. `WebhookReportSink` is the reference implementation: it POSTs `{"report": ...}` and
returns whatever `id` comes back.

Publishing is best-effort by construction — the report is durable in `report_backend`
before any sink runs, so a sink that is down must never cost the analysis. `publish` should
still RAISE on failure; the caller decides that it is survivable, and a sink that swallows
errors is indistinguishable from one that works.

## 8. MCP — both directions

- **Consume more** — `src/clients/mcp.py` connects to MCP servers; point it at additional
  servers to give agents new tool sources. The `handoff` server (the receiving
  platform this agent hands code-level work to, see `AE-HANDOFF-DESIGN.md`) is added
  conditionally when `HANDOFF_ENABLED=true`, following the same
  `observability`/`openchoreo` pattern — reuses the caller's `httpx.Auth`, no separate
  auth plumbing. Its tool NAMES are not in `tool_registry.py`: they are discovered
  generically from whatever the `handoff` connection advertises at request time. The
  per-run identity headers that connection carries are configured via
  `HANDOFF_HEADER_MAP` (`src/config.py`), which maps this agent's own context fields
  (`project`, `component`, `signature`, `action_statuses`) onto whatever header names
  the receiver expects.
- **Expose more** — `src/mcp_server.py` makes the agent itself an MCP server
  (`analyze_runtime_state`, `get_rca_report`). Add `@mcp_server.tool()` functions to expose
  new capabilities to upstream agents (e.g. the portal assistant). Auth is enforced by the
  ASGI middleware and re-checked per tool via `_authorize(...)`.

---

## Behavior to preserve when extending

- The **remediation agent recommends only** — its prompt forbids applying changes
  (`remed_agent_prompt.j2`: *"do not execute or apply any actions"*). Don't change this to
  auto-apply without an explicit design decision.
- The **handoff agent files only** — it creates one issue through the configured handoff
  MCP server and never merges, deploys or edits anything itself. What happens to that
  issue afterwards, including whether a coding agent picks it up and whether a resulting
  pull request needs human approval, is the receiving platform's policy and is not
  guaranteed here. Do not assume a human gate exists downstream unless that platform
  documents one.
- Analysis/remediation run as the agent's **service account** (`get_oauth2_auth()`); chat
  runs as the **user** (`BearerTokenAuth`). Preserve this identity split for new stages.
- Tools are an **allow-list** (read-only observability + scoped OpenChoreo reads, plus the
  single write tool `patch_releasebinding`). Keep new tools least-privilege.
