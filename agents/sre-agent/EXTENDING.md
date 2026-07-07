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
| Toggle the AE coding-agent handoff | `AE_HANDOFF` / `AE_AUTO_DISPATCH` env (`src/config.py`) | no |
| Expose new capabilities to upstream agents | `@mcp_server.tool()` in `src/mcp_server.py` | yes |

---

## 1. Configuration-only (no code)

Env-driven settings in `src/config.py` (loaded from `.env` / container env):

| Setting | Env var | Purpose |
|---|---|---|
| Model / provider | `RCA_MODEL_NAME` (e.g. `anthropic:claude-sonnet-4-6`) | provider-agnostic via `init_chat_model` |
| LLM key | `RCA_LLM_API_KEY` | |
| Report backend | `REPORT_BACKEND` (`sqlite`/`postgresql`), `SQL_BACKEND_URI` | storage selection |
| Remediation agent | `REMED_AGENT` (`true`/`false`) | enable the 2nd (revise-recommendations) agent |
| Concurrency / timeout | `max_concurrent_analyses`, `analysis_timeout_seconds` | |
| Data sources | `OBSERVER_API_URL`, `OPENCHOREO_API_URL`, `AE_API_URL` | MCP / API endpoints |
| Auth | OAuth2 / JWT vars + `auth-config.yaml` | authn/authz |

## 2. Prompt customization (Jinja templates, no logic)

- **Agent behavior** — `src/templates/prompts/{rca,remed,chat}_agent_prompt.j2`.
  Rewrite instructions, investigation strategy, recommendation rules, output guidance.
- **Telemetry formatting** — `src/templates/middleware/{logs,metrics,traces,trace_spans}.j2`.
  Control how raw observability data is shaped before it reaches the LLM.

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

## 8. MCP — both directions

- **Consume more** — `src/clients/mcp.py` connects to MCP servers; point it at additional
  servers to give agents new tool sources. The `ae` server (AE coding-agent integration,
  see `AE-HANDOFF-DESIGN.md`) is added conditionally when `AE_HANDOFF=true`, following the
  same `observability`/`openchoreo` pattern — reuses the caller's `httpx.Auth`, no separate
  auth plumbing.
- **Expose more** — `src/mcp_server.py` makes the agent itself an MCP server
  (`analyze_runtime_state`, `get_rca_report`). Add `@mcp_server.tool()` functions to expose
  new capabilities to upstream agents (e.g. the portal assistant). Auth is enforced by the
  ASGI middleware and re-checked per tool via `_authorize(...)`.

---

## Behavior to preserve when extending

- The **remediation agent recommends only** — its prompt forbids applying changes
  (`remed_agent_prompt.j2`: *"do not execute or apply any actions"*). Don't change this to
  auto-apply without an explicit design decision.
- The **handoff agent files/dispatches only** — it creates a GitHub issue and (if
  `AE_AUTO_DISPATCH=true`) dispatches the AE coding agent, but the coding agent stops at
  opening a PR. Nothing in this path auto-merges; PR review is the human gate. See
  `AE-HANDOFF-DESIGN.md`.
- Analysis/remediation run as the agent's **service account** (`get_oauth2_auth()`); chat
  runs as the **user** (`BearerTokenAuth`). Preserve this identity split for new stages.
- Tools are an **allow-list** (read-only observability + scoped OpenChoreo reads, plus the
  single write tool `patch_releasebinding`). Keep new tools least-privilege.
