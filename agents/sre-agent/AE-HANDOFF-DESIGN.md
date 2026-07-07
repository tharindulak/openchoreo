# Design: SRE Agent → AE Coding-Agent Handoff

**Status:** Implemented (both repos) — pending the shared-IdP confirmation in §9/§11 before
first real deployment
**Repos touched:** `openchoreo/agents/sre-agent` (this repo) and `labs-agentic-engineer` (AE)
**Author:** tharindulak (with Claude Code)

---

## 1. Goal

When an OpenChoreo (OC) alert triggers the SRE/RCA agent and the root cause requires a
**code-level** fix (not a config/ReleaseBinding change), the SRE agent should hand the work
off to the **Agentic Engineer (AE) coding agent** by:

1. Creating a GitHub issue in the project's repo (via AE), carrying the RCA context.
2. **Auto-dispatching** the AE coding agent against that issue.

The AE coding agent then finds and cross-links **related existing issues** (a capability the
SRE agent lacks today, because it never looks at the code repo) and opens a PR. The PR is the
human review gate — the coding agent never merges.

### Non-goals
- The RCA agent's telemetry investigation and scope-enforcement rules are **unchanged**. The
  handoff is a new downstream stage, exactly like the remediation agent was added.
- No auto-merge. The coding agent stops at "PR opened".

---

## 2. Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Where the "skill" logic lives | **Both sides** — SRE prompt+tools decide/create the issue and dispatch; AE-side `SKILL.md` does related-issue discovery/commenting during coding-agent execution |
| 2 | Shape of the "AE MCP" | **New thin TypeScript MCP server in the AE repo**, wrapping `aep-api` REST. SRE agent stays a pure MCP client |
| 3 | SRE → AE MCP auth | AE MCP accepts the **same service-account OAuth2 bearer** the RCA path already uses (`get_oauth2_auth()`) |
| 4 | Cross-system trust | **Shared / federated IdP** — the SRE service-account token carries an `ouHandle` claim that AE's `aep-api` trusts. No separate publisher client-credentials principal |
| 5 | OC → AE project/repo mapping | **AE resolves it** — SRE passes OC project/component; a new `aep-api` lookup resolves the AE project + GitHub repo |
| 6 | Handoff behavior | **Auto-dispatch** — create issue → dispatch coding agent. Flag-gated (default off), stops at PR-opened, recorded on the RCA report |
| 7 | Related-issue commenting | ~~AE-side `SKILL.md` in the coding-agent pod~~ **REVISED (2026-07-06): moved to the SRE-side handoff agent, body-mentions only.** It reads `ae_search_related_issues` results (full bodies) and writes a `## Related issues` section (`- #N — reason`) into the issue body it creates; GitHub's automatic cross-reference events on `#N` mentions provide the back-links, so no comments are posted on other issues at all. The AE-side `related-issues` skill, its `skillPreload` entry, and the `buildAgentPrompt` instruction are removed — issues arrive at the coding agent pre-linked |

---

## 3. Key facts that shaped this design

**SRE agent (`openchoreo/agents/sre-agent`)**
- LangChain/LangGraph. `Agent` factory + three stages: `RCA_AGENT`, `REMED_AGENT`, `CHAT_AGENT`
  (`src/agent/agent.py`).
- Already an MCP **client** to `observability` and `openchoreo` servers via
  `MultiServerMCPClient` (`src/clients/mcp.py`). Adding a third server is a 1-entry change.
- Tool allow-list + prompt-tool injection in `src/agent/tool_registry.py` and
  `Agent.create()`.
- `run_analysis()` (`src/agent/agent.py:270`) orchestrates: RCA → (optional) remediation →
  save. New stage slots in after remediation.
- Identity split (see `EXTENDING.md`): analysis/remediation run as the **service account**
  (`get_oauth2_auth()`); chat runs as the **user**. Handoff runs in the analysis path → uses
  the service-account token.
- Remediation model already separates config vs "needs something else":
  `RemediationAction.status == "revised"` carries a concrete `ResourceChange` (config-level);
  `status == "suggested"` has no `change` (candidate for code-level).
- **No GitHub integration exists.**

**AE (`labs-agentic-engineer`)**
- Go + TS monorepo. `aep-api` (Go) owns GitHub logic and per-org GitHub credentials
  (GitHub App or PAT). Coding agent = `remote-worker` pod using the Claude Agent SDK.
- **MCP was deliberately retired** in AE — the coding agent uses `git`/`gh` directly. So an
  "AE MCP" is a *new* server we build.
- **Issue operations are NOT exposed over REST today.** `IssueService`
  (`services/aep-api/internal/feature/gitrepo/issue_service.go`) has `CreateIssue`,
  `CommentIssue`, `EditIssueBody`, `ListIssues`, `CloseIssue` as **internal Go methods only**,
  called by `TaskService` during task generation. New REST endpoints must be added.
- Auth: public `/api/v1` requires an org-scoped **user JWT** (Huma `SecurityUserJWT`,
  `internal/platform/humakit/humakit.go`). Org comes from the verified claim
  (`OuHandle > OuName > OuId`), never from a path param.
- Coding-agent dispatch (`POST /api/v1/projects/{projectName}/tasks/dispatch`) expects a
  `ComponentTask` that already has an `issue_number` (created by AE's own generation flow). An
  ad-hoc issue from the SRE agent has **no task yet** → we need a "dispatch-from-issue" path.
- Coding agent loads skills via `skillPreload` in `runners/remote-worker/src/lib/runner.ts`;
  existing skill at `runners/remote-worker/plugin/skills/aep/SKILL.md`. It uses `gh` CLI for
  issue read/comment/PR.
- TS conventions: pnpm workspace, `tsx`/`tsc`, `node:http`/`https` (no axios), services under
  `services/`, Dockerfile + `deployments/docker-compose.yml` + k8s manifests.

---

## 4. End-to-end flow

```
OC alert fires
  └─► SRE agent  POST /api/v1alpha1/rca-agent/analyze            (existing)
        └─► RCA_AGENT      → RCAReport                            (existing, scope-pure)
        └─► REMED_AGENT    → config-level ResourceChanges         (existing, REMED_AGENT flag)
        └─► HANDOFF_AGENT  (NEW stage, AE_HANDOFF flag, RootCauseIdentified only)
              1. classify recommendations: config-level vs code-level
              2. if code-level exists:
                   ae_search_related_issues(...)   ── dedup / avoid duplicates
                   ae_create_issue(...)            ── RCA context + labels
                   ae_dispatch_coding_agent(...)   ── auto-dispatch (flag)
              3. record {issue_url, run_name, classification} on the RCA report
                        │
   ┌────────────────────┘  (crosses into the AE repo)
   ▼
AE aep-api  →  creates ComponentTask bound to the issue  →  dispatches K8s Job
   ▼
AE coding-agent pod (remote-worker, Claude Agent SDK)
   ├─ preloads: aep skill + NEW related-issues skill
   ├─ gh issue search → find related issues → comment/cross-link them
   ├─ implement code fix
   └─ gh pr create "Closes #<n>"        ← STOPS HERE (never merges)
```

---

## 5. Changes — Repo A: `labs-agentic-engineer` (AE)

### A1. Expose issue + dispatch REST endpoints in `aep-api` (Go) — *prerequisite*
Register new Huma ops wrapping the existing `IssueService`, on the org-scoped public surface:

- `POST /api/v1/projects/{projectName}/issues`
  → body `{ title, body, labels[] }` → `{ number, url, nodeId }`
- `GET  /api/v1/projects/{projectName}/issues?labels=&state=&q=`
  → `[{ number, title, body, url, state, labels[] }]`  (for dedup/related search)
- `POST /api/v1/projects/{projectName}/tasks/dispatch-from-issue`
  → body `{ issueNumber }` → creates a `ComponentTask` bound to the issue, then dispatches the
  coding-agent Job → `{ runName, status }`.
  *(Alternative: extend the existing `/tasks/dispatch` to accept a bare issue number and
  synthesize the task. Either way, the "task-from-ad-hoc-issue" gap must be filled.)*

**Mapping (decision #5):** these endpoints take `projectName` (OC project) and resolve the AE
project + owner/repo + org credentials internally (extend the existing repo/credential
resolution used by `IssueService`). The SRE agent never sees owner/repo.

**Auth (decision #4):** endpoints keep `SecurityUserJWT`. The SRE service-account token must
present an `ouHandle` claim from the shared/federated IdP that `aep-api` accepts. **Confirm the
IdP is shared and the service account is provisioned with the org claim** (see §9).

### A2. New TypeScript MCP server — `services/aep-mcp-server/`
- Streamable-HTTP MCP server (`@modelcontextprotocol/sdk`), pnpm workspace member,
  `node:http`/`https` client (matches repo convention).
- Reads `AEP_API_BASE_URL`; forwards the caller's bearer to `aep-api`.
- Tools exposed (contracts in §7): `ae_search_related_issues`, `ae_create_issue`,
  `ae_dispatch_coding_agent`.
- `Dockerfile` (node:22-alpine) + `deployments/docker-compose.yml` entry + k8s manifest,
  following the `aep-api` pattern.

### A3. New coding-agent SKILL — `runners/remote-worker/plugin/skills/related-issues/SKILL.md`
- Frontmatter (`name`, `description`) mirroring the existing `aep` skill; add its id to
  `skillPreload` in `runners/remote-worker/src/lib/runner.ts`.
- Instructs the agent, before/while implementing, to: search related issues
  (`gh issue list --search ...`), comment on and cross-link them to the current issue,
  respecting the existing deny-list (no force-push, no merge, PR only).
- This is the "look into related issues" capability the SRE agent cannot do (no repo access).

---

## 6. Changes — Repo B: `openchoreo/agents/sre-agent` (this repo)

### B1. Config — `src/config.py`
```python
ae_api_url: str = ""            # e.g. http://aep-mcp-server:3400 base
ae_handoff: bool = False        # gate the whole stage (mirrors remed_agent)
ae_auto_dispatch: bool = True   # if False: create issue only, no dispatch

@property
def ae_mcp_url(self) -> str:
    return f"{self.ae_api_url.rstrip('/')}/mcp"
```

### B2. MCP client — `src/clients/mcp.py`
Add a third server (only when `ae_handoff` is on) to `MultiServerMCPClient`, reusing the same
`auth` object already passed in:
```python
"ae": {
    "transport": "streamable_http",
    "url": settings.ae_mcp_url,
    "httpx_client_factory": _httpx_client_factory,
    "auth": auth,
}
```

### B3. Tool registry — `src/agent/tool_registry.py`
Add `AE = "ae"` server constant and `Tool` entries with active-forms:
`ae_search_related_issues`, `ae_create_issue`, `ae_dispatch_coding_agent`; group into
`AE_TOOLS`. Surface them in `Agent.create()`'s template context (add an `ae_tools` split).

### B4. New agent stage — `src/agent/agent.py` + `src/templates/prompts/handoff_agent_prompt.j2`
```python
HANDOFF_AGENT = Agent(
    template="prompts/handoff_agent_prompt.j2",
    tools={TOOLS.AE_SEARCH_RELATED_ISSUES, TOOLS.AE_CREATE_ISSUE, TOOLS.AE_DISPATCH_CODING_AGENT},
    middleware=[LoggingMiddleware, ToolErrorHandlerMiddleware],
    response_format=HandoffResult,     # new model, see B5
    recursion_limit=50,
)
```
Wire into `run_analysis()` after the remediation block, guarded by
`settings.ae_handoff and isinstance(rca_report.result, RootCauseIdentified)`. Wrap in
try/except like remediation so a handoff failure never fails the RCA report. Honor
`ae_auto_dispatch` (skip the dispatch tool when false).

The prompt encodes the **config-vs-code decision** (§8) and the create→dispatch sequence, and
tells the agent NOT to comment on related issues itself (the AE coding-agent skill does that).

### B5. Report model — `src/models/rca_report.py` (or a new `handoff_result.py`)
Add an optional block recording what was filed/dispatched, so the portal + audit trail show it:
```python
class HandoffResult(BaseModel):
    classification: Literal["config_level", "code_level", "mixed"]
    created_issue_url: str | None = None
    created_issue_number: int | None = None
    dispatch_run_name: str | None = None
    related_issue_urls: list[str] = []
    notes: str | None = None
```
Attach as `RCAReport.handoff: HandoffResult | None` and persist via `upsert_rca_report`.

---

## 7. MCP tool contracts (AE MCP server → `aep-api`)

| Tool | Input | Output | Backing REST (A1) |
|---|---|---|---|
| `ae_search_related_issues` | `project`, `query?` (space-separated keywords), `labels[]?` | `[{Number,Title,Body,URL,State,Labels}]` ranked by keyword overlap | `GET …/issues` |
| `ae_create_issue` | `project`, `title`, `body`, `labels[]?` | `{number, url, nodeId}` | `POST …/issues` |
| `ae_dispatch_coding_agent` | `project`, `componentName`, `title`, `issueNumber`, `issueUrl` | `{taskId, componentName, runName?, status, error?}` | `POST …/tasks/dispatch-from-issue` |

`project` = OC project (AE resolves repo/org). Bearer forwarded from the SRE agent.

`ae_dispatch_coding_agent` needs more than just the issue number: dispatch creates a brand-new
`ComponentTask` row for an ad-hoc issue (AE's own generation flow always creates the task first,
*then* the issue — an SRE-filed issue has no task yet), so `componentName` (must be a component
AE already knows about — see §11.6) and `title`/`issueUrl` are required to construct that row.

---

## 8. Config-vs-code decision logic

Encoded in `handoff_agent_prompt.j2`, seeded by the remediation output:

- **Config-level** (no issue): actionable as an OC ReleaseBinding change — i.e. remediation
  produced a `RemediationAction` with `status == "revised"` and a concrete `ResourceChange`
  (env var, replica count, resource limits, file-mount content, trait/componentType override).
- **Code-level** (→ issue + dispatch): requires a source change — e.g. add/adjust structured
  logging, fix a null deref, add a timeout/retry, correct business logic. Typically surfaces as
  a `status == "suggested"` action with no `change`, or an RCA root cause that no config knob
  can address.
- **Mixed:** apply config via the existing OC path AND file a code issue for the code part.

The prompt states this explicitly rather than relying purely on the `status` field, so the LLM
can justify the classification from the root cause.

---

## 9. Auth & identity — the crux (needs confirmation before A1)

Decision #4 = shared/federated IdP. Concretely this requires:
- OC's IdP (whatever issues the SRE service-account token via `get_oauth2_auth()`) and AE's
  Thunder to be the **same or federated**, so `aep-api` can verify the token against its
  configured issuer/JWKS/audience.
- The SRE **service account** to carry an `ouHandle` (org) claim identifying the AE org.

**Open confirmations (see §11):** is the IdP actually shared today? What issuer/audience does
`aep-api` expect, and can the SRE service-account token match it? If not, fallback is the
publisher client-credentials path (a separate AE Thunder principal used only by the MCP
server) — noted here so we can pivot without redesign.

---

## 10. Safety & WSO2 org-policy alignment

- **Auto-dispatch is flag-gated** (`AE_HANDOFF`, `AE_AUTO_DISPATCH`), default conservative, so
  the behavior is opt-in per deployment.
- **No auto-merge** — the coding agent opens a PR only; human review/merge is the gate. This
  keeps a human in the loop for the code change, per WSO2 "human review before action".
- **Least privilege** — new MCP tools are the only write surface added to the SRE agent; GitHub
  credentials stay in AE (the SRE agent never holds them). Matches the "tools are an allow-list"
  principle in `EXTENDING.md`.
- **Auditability** — every created issue + dispatch is recorded on the RCA report (B5).
- **Secrets** — the SRE token is forwarded, not logged; MCP server masks bearer in logs.

---

## 11. Open items / prerequisites

### Resolved during implementation

- **`dispatch-from-issue`** — implemented as `DispatchService.DispatchFromIssue` in
  `codingagent/dispatch_service.go`, exposed via `POST /projects/{projectName}/tasks/dispatch-from-issue`
  in `task/task_huma.go`. It creates a `ComponentTask` with no `BatchID`/`DependsOnComponents`
  (dispatchOne gates on neither — confirmed by reading `DispatchTasks`/`RetryTask`), then calls the
  same `dispatchOne` primitive both of those use. No new dependencies needed — `taskRepo.Create` +
  the existing `repoSvc`/`credSvc` were sufficient.
- **OC→AE project resolution** — turned out to need no new resolution layer. Every existing AE
  endpoint (`list-tasks`, `dispatch-tasks`, etc.) already uses the project's slug/name directly as
  its DB key (`ListByProjectID(orgID, projectName)` — the param is *called* `projectID` but *is*
  the slug). So the SRE agent's `scope.project` passes straight through as `project` — no mapping
  needed, provided OC and AE project names/slugs match (assumed, per decision #5 — not yet
  verified against a real paired OC+AE deployment).
- **Issue labels** — not implemented as a fixed convention; `ae_create_issue`/`CreateIssueRequest`
  accepts an arbitrary `labels[]`, left to the handoff prompt / operator to decide. No hardcoded
  `sre-agent` label yet — add one later if dedup/discovery needs it.
- **MCP server port/placement** — `services/aep-mcp-server/`, port 3400 internal / 3401 host,
  wired into `deployments/docker-compose.yml` (depends on `aep-api`, same `aep` network). Verified
  with a real `docker build` + standalone container run (see §13).

### Resolved during the live E2E run (2026-07-03)

- **Shared IdP: CONFIRMED empirically.** The RCA agent's `openchoreo-rca-agent`
  client-credentials token (k3d Thunder) carries `iss=http://thunder.openchoreo.localhost:8080`
  (matching `aep-api`'s `JWT_ISSUER`) **and `ouHandle: default`** — the org claim `aep-api`'s
  tenant gate needs. The ONLY accommodation required was audience: the cc token's
  `aud=openchoreo-rca-agent` doesn't match `aep-*`, fixed by extending the compose default to
  `JWT_AUDIENCE: aep-*,openchoreo-rca-agent` (comma-list is supported via `SplitAndTrim`).
- **OC scoped-name vs AE component name.** OC's alert scope carries the *scoped* component name
  (`<project>-<component>`, e.g. `demoservices-service1`) while AE keys components unprefixed
  (`service1`). Handled in `handoff_agent_prompt.j2` (strip the `<project>-` prefix for
  `ae_dispatch_coding_agent.componentName`) — verified working: the created `ComponentTask` row
  shows `service1`.
- **Live E2E verified**: alert-triggered RCA → remediation → handoff → GitHub issue
  `demoservices474#5` (correct title, labels, real telemetry in body) → `dispatch-from-issue` →
  coding-agent Job running. `classification=mixed` was correct (2 revised config actions +
  1 suggested code action → single issue for the code part).

### Resolved

- **Duplicate-dispatch race — FIXED (2026-07-06) with two layers.** Two concurrent RCA runs for
  one incident each ran a handoff; the second's `ae_search_related_issues` ran before/while the
  first's issue was created and missed it → duplicate issue + second coding-agent dispatch
  (helloserv #16–#19; badbackend #5/#6). Search-then-create is not atomic, so no search
  improvement alone can fix it. The fix pairs:
  1. **Dedup = correctness layer (`dedupeKey`).** `ae_create_issue` takes a stable
     `dedupeKey` (`sre-rca/<component>`); aep-api maps it to a `dedupe:<key>` label and, under a
     per-repo lock (`issueService.createLocks`), returns any existing OPEN issue with that label
     (`deduped: true`) instead of creating a duplicate. The handoff skips dispatch on
     `deduped: true`. In-process atomic (single aep-api instance = the deployment); across
     replicas the window shrinks to one list+create roundtrip (a DB unique constraint would close
     that fully). One open issue per incident regardless of timing.
  2. **Rule hygiene + suppression = prevention layer.** `ALERT_SUPPRESSION_WINDOW=1h` de-dups
     repeated fires of one rule per component; one rule per condition per component (no rotated
     `-r2`/`-r3` duplicates) keeps concurrent triggers rare. Reduces the odds; dedup makes the
     outcome correct when they still collide.
2. **No retry on the agent's OAuth token fetch (found live, 2026-07-05).** `MCPClient` auth uses
   authlib client-credentials with no retry/timeout tuning: when the local k3d VM was CPU-starved
   (concurrent Go image build + 560MB image import), the Thunder token POST hit
   `httpx.ReadTimeout` and BOTH in-flight analyses hard-failed — and because the observer had
   already consumed the alert, suppression then blocked a re-fire for the full window, silently
   dropping the incident. Fix candidates: retry-with-backoff around `_fetch_token`, a longer
   token-fetch timeout, and/or the observer only recording suppression state after a 2xx from
   `/analyze` *completion* rather than acceptance.
3. **`skills:` in the Claude Agent SDK is an enablement filter, NOT a preload (found live).**
   The runner listing `aep:related-issues` in `skills:` only makes it *invocable* — the agent
   won't reliably use it unprompted. The working mechanism (same as the `aep` skill): name it
   explicitly in the dispatch prompt (`buildAgentPrompt`, `codingagent/dispatch_service.go`).
2. **`ensureOCComponent` requires the component to already exist in AE.** Discovered while
   implementing `DispatchFromIssue`: `dispatchOne` → `ensureOCComponent` →
   `artifacts.ResolveDesignComponent` reads the component's design doc from
   `specs/design/components/<componentName>/` in AE's artifact store — it does not create one from
   scratch. This means the handoff **only works for components AE itself originally created**
   (via its own generation flow) — not for components deployed by other means that happen to share
   a name. This is a reasonable v1 scope boundary (AE-created components are exactly what an
   SRE/RCA agent would be filing a code-level issue against), but worth stating explicitly:
   `ae_dispatch_coding_agent` will fail with "ensure OC component: resolve component: ..." for any
   other component.
---

## 12. Build order (as executed)

1. ~~AE A1~~ — REST endpoints for issue create/list + dispatch-from-issue. Done.
2. ~~AE A2~~ — TS MCP server wrapping A1. Done.
3. ~~SRE B1–B5~~ — config + MCP client + tool registry + handoff stage + prompt + report model.
   Done, flag-gated (`AE_HANDOFF=false` default).
4. ~~AE A3~~ — related-issues SKILL.md, force-preloaded alongside `aep:aep`. Done.
5. ~~Live E2E~~ — done 2026-07-03: alert → RCA → handoff → issue `demoservices474#5` →
   coding-agent dispatch, on the k3d+compose paired stack (see §11 "Resolved during the live
   E2E run" and §14).
6. **Not yet done:** update `EXTENDING.md`-equivalent AE docs (this repo's `EXTENDING.md` was
   already updated for the SRE side).

---

## 14. Enabling the handoff on a k3d OC + docker-compose AE stack (as tested)

1. Build the SRE-agent image from this repo (includes the handoff code) and import it:
   `docker build -t openchoreo-sre-agent:handoff .` → `k3d image import openchoreo-sre-agent:handoff -c <cluster>`.
2. `kubectl patch cm rca-agent-config -n openchoreo-observability-plane --type=merge -p
   '{"data":{"AE_HANDOFF":"true","AE_AUTO_DISPATCH":"true","AE_API_URL":"http://host.k3d.internal:3401"}}'`
   (`host.k3d.internal` reaches the host's docker-compose `aep-mcp-server` on 3401.)
3. `kubectl set image deploy/ai-rca-agent -n openchoreo-observability-plane "*=openchoreo-sre-agent:handoff"`.
4. AE side: extend `aep-api`'s `JWT_AUDIENCE` with `openchoreo-rca-agent` (see §11) and
   `docker compose up -d aep-api`.
5. Safety: ensure `ALERT_SUPPRESSION_WINDOW` is set in `observer-config` (see §11 race note),
   and remember the OC project/component must exist in AE (same project slug; AE-created
   component).
6. Verify at agent startup: `MCP connection successful: loaded 102 tools` (99 + the 3 `ae_*`
   tools), then trigger and watch for `Running handoff agent` → `Handoff completed:
   classification=…, issue=…, dispatch=…` in the agent logs.

## 13. Verification performed

No unit-test suite existed for either "SRE unit-test the classification prompt" or "AE contract
tests for the new endpoints" as originally planned below — instead, verification leaned on the
existing toolchains plus live runtime checks, since both repos had working build/test
infrastructure already in place:

- **SRE (Python):** import of `HANDOFF_AGENT`/`HandoffResult`/`AE_TOOLS`; config validator
  correctly rejects `AE_HANDOFF=true` without `AE_API_URL`; Jinja template renders correctly for
  both `auto_dispatch` branches; `ruff` clean on all changed files.
- **AE Go:** `go build ./...`, `go vet ./...`, and the full `go test ./...` suite (including the
  `internal/arch` cycle-lock test, which would fail if `task`→`codingagent` became a real import)
  all pass with the new `DispatchFromIssue` method and `issue_huma.go` endpoints in place.
- **AE TypeScript (`aep-mcp-server`):** `tsc --noEmit` clean against the real installed
  `@modelcontextprotocol/sdk`; `eslint` clean; a live run (`tsx src/main.ts`) that: rejects
  requests with no `Authorization` header (401), completes a real MCP `initialize` handshake,
  returns `tools/list` with the three correctly-shaped tool schemas, and surfaces a downstream
  connection failure as a clean tool-level error rather than crashing.
- **AE Docker:** the real multi-stage `Dockerfile` (using `pnpm deploy --legacy` to avoid
  dangling-symlink `node_modules` in the runtime stage) was built with `docker build` from repo
  root context and run standalone — `/healthz` responds correctly with zero monorepo context
  present in the container.
- **AE `docker-compose.yml`:** `docker compose config` validates the new `aep-mcp-server` service
  definition.
- **`runner.ts` skill wiring:** `remote-worker`'s typecheck and existing test suite pass with
  `aep:related-issues` added to the preload array.

**Explicitly not done** (would create real side effects — see §11 item 3): no call was made
against the user's live local `aep-api` container to actually create a GitHub issue or dispatch
a real coding-agent run. That requires the user's explicit go-ahead.
