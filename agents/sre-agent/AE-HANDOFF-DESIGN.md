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
SRE agent lacks today, because it never looks at the code repo) and opens a PR.

**Correction (2026-08-19):** this document originally said "the PR is the human review gate —
the coding agent never merges" and "No auto-merge. The coding agent stops at 'PR opened'". The
first half is true and the second was misleading: the *agent* never merges, but AE's **merge
policy does** (`eventcore/merge.go` squash-merges a pull request that resolves its run's
milestone work), so an incident fix reached production with no human in it. AEP's ADR-0018 adds
the human gate this text assumed — see §15.

### Non-goals
- The RCA agent's telemetry investigation and scope-enforcement rules are **unchanged**. The
  handoff is a new downstream stage, exactly like the remediation agent was added.

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
              1. decide: does the root cause need a source code change?
                   (classification is DERIVED from that answer + statuses)
              2. if it does:
                   ae_search_related_issues(...)   ── dedup / avoid duplicates
                   ae_create_issue(...)            ── RCA context + labels, and
                                                      adopt (default; the
                                                      AE_AUTO_DISPATCH flag)
              3. record {issue_url, adopted, classification} on the RCA report
                        │
   ┌────────────────────┘  (crosses into the AE repo)
   ▼
AE aep-api  →  files the issue into the deployed version's milestone as agent
               work  →  starts (or wakes) the milestone's coding run
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

### A2. Handoff MCP surface — `aep-api` `POST /sre-mcp` (in-process)

> **Updated:** the handoff tools were originally shipped as a standalone
> TypeScript server (`services/aep-mcp-server`, `@modelcontextprotocol/sdk`,
> port 3400/3401) that forwarded the caller's bearer to `aep-api`'s REST
> endpoints. That server has since been **merged into `aep-api`**: the same 3
> tools are now served in-process at `POST /sre-mcp`
> (`services/aep-api/internal/feature/handoff`), calling aep-api's issue/dispatch
> services directly (no self-HTTP hop, no separate deployable). The description
> below reflects the current topology.

- A hand-rolled JSON-RPC (Streamable-HTTP, single-response) mount on `aep-api`,
  behind the same public-edge `jwt` + `ensureOrg` middleware as the REST API.
- The caller presents its Thunder token (`aud openchoreo-rca-agent`); the acting
  org is bound from the verified `ouHandle` claim (never the request).
- Tools exposed (contracts in §7): `ae_search_related_issues` and
  `ae_create_issue`. There is no dispatch tool — creating an issue adopts it (see
  §7 and AEP's ADR-0017), so filing and handing over cannot come apart.

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
ae_api_url: str = ""            # aep-api base, e.g. http://aep-api:9090
ae_handoff: bool = False        # gate the whole stage (mirrors remed_agent)
ae_auto_dispatch: bool = True   # if False: create issue only, no dispatch

@property
def ae_mcp_url(self) -> str:
    # aep-api serves the handoff tools in-process at /sre-mcp.
    return f"{self.ae_api_url.rstrip('/')}/sre-mcp"
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
`ae_search_related_issues`, `ae_create_issue`; group into `AE_TOOLS`. Surface them
in `Agent.create()`'s template context (add an `ae_tools` split).

### B4. New agent stage — `src/agent/agent.py` + `src/templates/prompts/handoff_agent_prompt.j2`
```python
HANDOFF_AGENT = Agent(
    template="prompts/handoff_agent_prompt.j2",
    tools={TOOLS.AE_SEARCH_RELATED_ISSUES, TOOLS.AE_CREATE_ISSUE},
    middleware=[LoggingMiddleware, ToolErrorHandlerMiddleware],
    response_format=HandoffResult,     # new model, see B5
    recursion_limit=50,
)
```
Wire into `run_analysis()` after the remediation block, guarded by
`settings.ae_handoff and isinstance(rca_report.result, RootCauseIdentified)`. Wrap in
try/except like remediation so a handoff failure never fails the RCA report.

`ae_auto_dispatch` is honoured in CODE, not by the prompt: `_wrap_create_issue`
sets `adopt` on the create call from the flag, so the model cannot adopt against
the operator's wishes and cannot forget to. The same wrapper forces `dedupeKey`,
the `sre-agent` label, and the unprefixed design `componentName`, and records
what the call answered (see B5).

The prompt encodes the **config-vs-code decision** (§8), and tells the agent NOT to
comment on related issues itself (the AE coding-agent skill does that).

### B5. Report model — `src/models/rca_report.py` (or a new `handoff_result.py`)
Add an optional block recording what was filed/dispatched, so the portal + audit trail show it:
Two models, because two different things are being recorded — a judgment and a
set of facts:
```python
class HandoffJudgment(BaseModel):     # the LLM's response_format
    needs_code_change: bool           # the ONE question it answers
    rationale: str
    related_issues: list[RelatedIssue] = []

class HandoffResult(BaseModel):       # the persisted record
    classification: Literal["config_level", "code_level", "mixed", "none"]
    created_issue_url: str | None = None
    created_issue_number: int | None = None
    adopted: bool = False              # AE has the issue and a run is on it
    adoption_error: str | None = None  # why not, when not
    related_issues: list[RelatedIssue] = []
    rationale: str
```
`classification` is absent from the LLM's schema on purpose: it is DERIVED
(`derive_classification`) from `needs_code_change` plus remediation's statuses —
`none` when no code change is needed, `mixed` when some action was already
`revised` into a ReleaseBinding change, `code_level` otherwise. Asking a model to
restate a boolean over a field it was just handed only lets it disagree with the
data.

The issue/adoption fields are STAMPED from `ae_create_issue`'s answer
(`apply_handoff_facts`), not restated by the model: the console's Alerts list
serves this snapshot as-is, so a loose restatement would show a human the wrong
dispatch state while they triage.
Attach as `RCAReport.handoff: HandoffResult | None` and persist via `upsert_rca_report`.

---

## 7. MCP tool contracts (AE MCP server → `aep-api`)

| Tool | Input | Output | Backing REST (A1) |
|---|---|---|---|
| `ae_search_related_issues` | `project`, `query?` (space-separated keywords), `labels[]?` | `[{Number,Title,Body,URL,State,Labels}]` ranked by keyword overlap | `GET …/issues` |
| `ae_create_issue` | `project`, `title`, `body`, `labels[]?`, `componentName?`, `dedupeKey?`, `adopt?` | `{number, url, nodeId, deduped?, adopted?, adoptionError?}` | `POST …/issues` |

`project` = OC project (AE resolves repo/org). Bearer forwarded from the SRE agent.

**Creating the issue IS the dispatch.** `adopt` defaults to true: AE files the issue
into the deployed version's milestone (or the spec build in flight when nothing is
deployed yet) as agent work, in ONE GitHub write, then starts or wakes that
milestone's run. `adopt: false` files a ledger entry instead — recorded against the
version, worked by nobody until a human adds `aep:codingagent`.

`componentName` must be the name AE's DESIGN uses — unprefixed (`service1`, not
`demohello-service1`, see §11.6). It is checked before the issue is filed, so a wrong
name fails the call instead of surfacing later inside a coding cycle.

The answer carries the two things the caller cannot work out for itself: `deduped`
(an open issue with the same key already existed, and its own run owns its dispatch)
and `adopted` / `adoptionError` (whether anything will actually work this issue —
a project with nothing built yet gets the issue recorded but not adopted).

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

The LLM is asked for one thing — `needs_code_change` — and `status` is a strong signal for it
rather than the rule, so a root cause no config knob can address still files even when the
remediation agent revised something. The config/code/mixed LABEL is then derived, not judged;
see B5.

Two things are withheld from the payload the model sees (`handoff_input`), both boundaries rather
than instructions. `observability_recommendations` goes entirely — advice for making future
analyses easier is not this incident's fix, and it is the main source of issues a coding agent
cannot act on. And the `change` patch is stripped from every already-`revised` action: the issue
becomes a coding agent's prompt, that agent can only edit the repository, and a concrete
ReleaseBinding patch in front of it invites a pull request expressing config as code. The
action's description and status stay, so the issue can say "this part was already fixed by
configuration" instead of asking for it again.

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
- **MCP surface placement** — originally a standalone `services/aep-mcp-server` (port 3400/3401,
  its own compose service). Now merged into `aep-api` and served in-process at `POST /sre-mcp`
  (see the A2 update note); the separate container/port are gone.

### Resolved during the live E2E run (2026-07-03)

- **Shared IdP: CONFIRMED empirically.** The RCA agent's `openchoreo-rca-agent`
  client-credentials token (k3d Thunder) carries `iss=http://thunder.openchoreo.localhost:8080`
  (matching `aep-api`'s `JWT_ISSUER`) **and `ouHandle: default`** — the org claim `aep-api`'s
  tenant gate needs. The ONLY accommodation required was audience: the cc token's
  `aud=openchoreo-rca-agent` doesn't match `aep-*`, fixed by extending the compose default to
  `JWT_AUDIENCE: aep-*,openchoreo-rca-agent` (comma-list is supported via `SplitAndTrim`).
- **OC scoped-name vs AE component name — now AE's to resolve (2026-08-31).** OC's alert scope
  carries the *scoped* component name (`<project>-<component>`, e.g. `demoservices-service1`)
  while AE keys components unprefixed (`service1`). This repo used to strip the prefix itself
  (`design_component_name`) because the model kept copying the prefixed name out of the related
  issues it had just read. The stripping is gone: `_wrap_create_issue` still FORCES
  `ae_create_issue.componentName` to the alerting component — that part was never about naming,
  it is about the fact being the alert's rather than the model's — but sends the OpenChoreo name
  verbatim, and AE resolves it against its own design (`eventcore.ensureNamedComponent`, which
  tries the name as given first and only then without the prefix). The convention belongs to the
  side that owns the design; a copy of it here was a copy in the wrong repo. See §16.
- **Live E2E verified**: alert-triggered RCA → remediation → handoff → GitHub issue
  `demoservices474#5` (correct title, labels, real telemetry in body) → coding-agent Job running.
  `classification=mixed` was correct (2 revised config actions + 1 suggested code action → single
  issue for the code part). Verified when dispatch was still a second call
  (`dispatch-from-issue`); the same loop now completes on `ae_create_issue` alone.

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
   `ae_create_issue` will fail with "ensure OC component: resolve component: ..." for any other
   component — a 400 naming the component, before the issue is filed.
---

## 12. Build order (as executed)

1. ~~AE A1~~ — REST endpoints for issue create/list + dispatch-from-issue. Done; the
   dispatch-from-issue endpoint was later retired when adoption moved into create-issue
   (AEP ADR-0017).
2. ~~AE A2~~ — TS MCP server wrapping A1. Done.
3. ~~SRE B1–B5~~ — config + MCP client + tool registry + handoff stage + prompt + report model.
   Done, flag-gated (`AE_HANDOFF=false` default).
4. ~~AE A3~~ — related-issues SKILL.md, force-preloaded alongside `aep:aep`. Done.
5. ~~Live E2E~~ — done 2026-07-03: alert → RCA → handoff → issue `demoservices474#5` →
   coding-agent dispatch, on the k3d+compose paired stack (see §11 "Resolved during the live
   E2E run" and §13).
6. **Not yet done:** update `EXTENDING.md`-equivalent AE docs (this repo's `EXTENDING.md` was
   already updated for the SRE side).

---

## 13. Enabling the handoff on a k3d OC + docker-compose AE stack (as tested)

1. Build the SRE-agent image from this repo (includes the handoff code) and import it. Use the
   FULLY QUALIFIED name AEP's installers default to, so a later `setup-observability.sh` run
   picks up this local build instead of pulling — an unqualified tag resolves to
   `docker.io/library/<name>` once containerd evicts it, and fails:
   `cd <openchoreo>/agents && docker build -t tharindulak/sre-agent:handoff-provider -f sre-agent/Dockerfile .`
   → `k3d image import tharindulak/sre-agent:handoff-provider -c <cluster>`.
   The build context is `agents/`, NOT `agents/sre-agent`: the Dockerfile pulls in the shared
   `agents/common` package (upstream PR #4372), so building from inside `sre-agent/` cannot
   resolve its COPY paths.
2. Mount the receiving platform's provider descriptor — the handoff refuses to run without it,
   because its tool and argument names are what make a filed issue dedupe and get handed over:
   `kubectl create configmap rca-agent-handoff-provider -n openchoreo-observability-plane
   --from-file=provider.json=<aep>/services/aep-mcp-server/handoff/provider.json`, then patch the
   Deployment with a volume mounting it at `/etc/rca-agent/handoff`.
   `setup-observability.sh` step 3d does both, alongside the skill mount.
3. `kubectl patch cm rca-agent-config -n openchoreo-observability-plane --type=merge -p
   '{"data":{"HANDOFF_ENABLED":"true","HANDOFF_HAND_OVER":"true",
   "HANDOFF_API_URL":"http://host.k3d.internal:3401",
   "HANDOFF_PROVIDER_FILE":"/etc/rca-agent/handoff/provider.json",
   "REPORT_SINK":"webhook","REPORT_SINK_URL":"http://host.k3d.internal:9090/api/v1/rca-agent/reports"}}'`
   (`HANDOFF_API_URL` must point at `aep-mcp-server` — `:3401` — and the agent appends
   `HANDOFF_MCP_PATH`, default `/mcp`. Reports go somewhere else entirely: `REPORT_SINK_URL` is
   aep-api's REST endpoint on `:9090`, and it is the FULL url, not a base — the sink posts exactly
   there. Omit those two and reports go nowhere, which empties the console Alerts list SILENTLY,
   because publishing is best-effort by design.
   These keys replaced `AE_HANDOFF` / `AE_AUTO_DISPATCH` / `AE_API_URL` / `AE_PUBLISH_REPORTS` /
   `AEP_API_URL` when the handoff stopped carrying AEP's vocabulary. `setup-observability.sh`
   writes BOTH sets so an image from either side of the rename works — otherwise a fallback to an
   older tag leaves `HANDOFF_*` set, `AE_HANDOFF` unset, and the handoff silently off.)
4. `kubectl set image deploy/ai-rca-agent -n openchoreo-observability-plane "*=tharindulak/sre-agent:handoff-provider"`.
5. AE side: extend `aep-api`'s `JWT_AUDIENCE` with `openchoreo-rca-agent` (see §11) and
   `docker compose up -d aep-api`.
6. Safety: ensure `ALERT_SUPPRESSION_WINDOW` is set in `observer-config` (see §11 race note),
   and remember the OC project/component must exist in AE (same project slug; AE-created
   component).
7. Verify at agent startup: `MCP connection successful: loaded N tools` (the base set + the 2
   handoff tools the descriptor names) and `Handoff provider loaded from
   /etc/rca-agent/handoff/provider.json`, then trigger and watch for `Running handoff agent` →
   `Handoff completed:
   classification=…, issue=…, adopted=…` in the agent logs.

## 14. Verification performed

The originally planned "SRE unit-test the classification prompt" was overtaken: there is no
classification prompt to test any more, because the classification is derived. What the SRE side
does have now is `tests/test_classification.py` (the derivation table, the payload boundary, and
the composer) and `tests/test_handoff_wrappers.py` (the code-enforced create-issue wrapper),
alongside `test_fingerprint.py` and `test_skills.py`. "AE contract tests for the new endpoints"
remains open. The rest of the verification leaned on the existing toolchains plus live runtime
checks, since both repos had working build/test infrastructure already in place:

- **SRE (Python):** import of `HANDOFF_AGENT`/`HandoffResult`/`AE_TOOLS`; config validator
  correctly rejects `AE_HANDOFF=true` without `AE_API_URL`; Jinja template renders correctly for
  both `auto_dispatch` branches; `ruff` clean on all changed files.
- **AE Go:** `go build ./...`, `go vet ./...`, and the full `go test ./...` suite (including the
  `internal/arch` cycle-lock test, which would fail if `task`→`codingagent` became a real import)
  all pass with the new `DispatchFromIssue` method and `issue_huma.go` endpoints in place.
- **AE TypeScript (`aep-mcp-server`):** _(historical — this standalone server was later merged
  into aep-api's `POST /sre-mcp` Go handler; see the A2 update note. The handoff tools are now
  covered by Go tests in `services/aep-api/internal/feature/handoff`.)_ At the time: `tsc
  --noEmit` clean against the real installed `@modelcontextprotocol/sdk`; `eslint` clean; a live
  run (`tsx src/main.ts`) that rejected requests with no `Authorization` header (401), completed a
  real MCP `initialize` handshake, returned `tools/list` with the three correctly-shaped tool
  schemas, and surfaced a downstream connection failure as a clean tool-level error rather than
  crashing.
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

---

## 15. Recurrence and the confidence gate (2026-08-19)

Two changes on the AE side, both recorded in AEP's **ADR-0018 — "a merged fix is not a
resolved incident"**. Nothing in this repo's contract changes: the handoff still makes ONE
`ae_create_issue` call with the same forced `dedupeKey`, `componentName`, `adopt` and
`sre-agent` label.

### 12.1 A recurrence reopens

Closing an issue on merge asserts the incident is over — something nothing had observed. AE's
dedupe lookup previously matched only OPEN issues, so once a fix merged and closed the issue,
the next occurrence of the identical fingerprint filed a *fresh* issue and the failed attempt's
history was lost.

Now, on the same `dedupeKey`:

| Match | Outcome |
|---|---|
| An **open** issue | dedupe, unchanged — that issue's run owns its dispatch |
| A **closed, `completed`** issue carrying `sre-agent` | **recurrence** — reopened |
| A **closed, `not_planned`** issue | never reopened; a human decided, and a fresh issue is filed |

A recurrence appends `## Recurrence <n>` with this call's body as the new evidence, reopens the
issue, re-homes it into the currently deployed version's milestone, and re-adopts it. There is
no time bound.

**What this repo consumes:** `ae_create_issue` answers with `reopened: true` and `recurrence`
(which attempt this is). Both are stamped onto `HandoffResult` by `apply_handoff_facts` — never
restated by the model, like `adopted`/`adoption_error` — and `recurrence` is forwarded on the
RCA report so the console can show "Attempt 3". The handoff never decides a recurrence; AE does,
deterministically.

The loop **never gives up**: past attempt 3 a recurrence is escalated, which means it is
reported loudly and worked anyway.

### 12.2 Incident fixes declare their confidence

AE's coding agent now writes `Confidence: high|low` into the pull request body of any PR
resolving an `sre-agent` issue. `high` merges as before; `low`, missing or unparseable **holds**
the PR for a human, and the run parks without spending re-dispatch budget. Spec builds are
unaffected.

This is the human gate §1 originally (wrongly) claimed already existed. It lives entirely in
AEP; nothing in the SRE agent needs to know about it, beyond the fact that a filed incident may
now take longer to reach production because a person is in the path.

---

## 16. AE's conventions moved to AE (2026-08-31)

Two things this repo enforced were facts about AE's own model, re-derived here from
conventions it does not own. Both now live on the AEP side, and `handoff_logic.py` is
smaller by exactly the amount that was never its business.

### 13.1 The component name is resolved by the side that owns the design

`design_component_name` is **deleted**. It stripped the `<project>-` prefix off the alert
scope's component so `ae_create_issue.componentName` would match AE's design names.

AE now does that itself, in `eventcore.ensureNamedComponent`: it tries the name **as given**
first — so a design that genuinely carries a hyphenated `<project>-<name>` component still
resolves to itself — and retries without the prefix only for a name the design does not carry,
and only on the resolution sentinel. A name missing under both forms is still refused before
anything is filed, naming the string the caller actually sent.

That is strictly better than what was here: this repo's version stripped unconditionally, so a
component genuinely named `<project>-foo` would have been mangled. And it fixes the same bug for
every caller of create-issue rather than for this one agent — aep-api had a second copy of the
strip in its own escalation path (`ops/createreport.unprefixedComponent`), which is what a
convention living on the wrong side of a boundary looks like.

`_wrap_create_issue` still FORCES `componentName`. That was never about naming: the alerting
component is a fact about the incident, and the model used to answer with whatever name it had
just read out of a related issue. It now sends the OpenChoreo name verbatim.

### 13.2 AE's planned-work issues arrive already marked

`annotate_platform_issues`, `PLATFORM_WORK_LABEL`, `PLATFORM_ISSUE_NOTE` and
`_wrap_search_related_issues` are **deleted**. They marked any `aep`-labelled issue in a search
result as AE's own plan for what to BUILD — the annotation that stopped "Implement service2 slow
backend" being read as evidence that a slow service2 is fine.

`ae_search_related_issues` now returns those records already carrying `PlatformRecord: true` and
`ReadAs` (`aep-mcp-server/src/platformIssues.ts`). The `aep` label is AE's arming switch
(`delivery.LabelAgentWork`); what it means is AE's to say, and this repo was holding a
hardcoded copy of a string it does not define.

Nothing about the handoff's behaviour changes. The model reads the same fields off the same
search results; they are simply stamped one hop earlier, by the side that knows what they mean.

### 13.3 What did NOT move, and why

The `sre-agent` label is still applied here, in `_wrap_create_issue`. Moving it needs AE to
identify the CALLER, and aep-api's verified `Claims` carries no audience — only `Subject`,
`ClientID` and the org claims — so a server-side stamp would assert provenance it cannot check,
which is no better than the caller self-reporting it and worse for being invisible. The label is
load-bearing (it gates `eventcore/unverified.go` and ADR-0018's reopen-on-recurrence), so
mislabelling another caller's issue would have real consequences. It moves when there is a
verified caller identity to key it off, not before.

### 13.4 Deploy order

aep-api must be deployed **before** this agent. The removals here make the agent send an
OpenChoreo-prefixed `componentName` and stop annotating search results; an aep-api that predates
`ensureNamedComponent` refuses that name with a 400 and files nothing — and nothing retries a
handoff. The reverse order is safe: the old agent's already-stripped name resolves as-given under
the new aep-api, and its own annotation is simply redundant with the server's.

---

## 17. Report publishing became a generic sink (2026-08-31)

`src/clients/aep_reports.py` is **deleted** — all 275 lines. It was the last piece of
this repo that spoke AEP's contract: it spelled `/api/v1/rca-agent/reports`, mapped
AEP's request field names, translated the classification enum's underscores to AEP's
hyphens, and rendered the Markdown layout of AEP's console. Carrying that in an
Apache-2.0 upstream project made every AEP contract change a change to somebody else's
repository, and `agents/common/` arriving upstream (PR #4372) made the generic/specific
line explicit enough that it could no longer be ignored.

### 14.1 What this repo has now

`src/clients/sink/` — `ReportSink`, one abstract method, plus `get_report_sink()` keyed on
`REPORT_SINK`. `WebhookReportSink` POSTs `{"report": <the agent's own report>}` to
`REPORT_SINK_URL` with the same OAuth2 service-account credentials the handoff already
uses. It maps nothing. Config: `AE_PUBLISH_REPORTS` and `AEP_API_URL` are gone, replaced by
`REPORT_SINK` / `REPORT_SINK_URL`.

`should_publish_report` moved to `handoff_logic.py`. It is one rule — a handoff that
**deduped** folded this incident onto an issue an earlier run already filed, and that run
already published its own report; a second row would read as unworked while a coding agent
is on it. It reads the handoff result, which is what `handoff_logic` owns, and keeping it
out of the sink is what leaves the sink provider-neutral.

### 14.2 What AEP has now

`internal/ops/createreport/native.go` derives every column from the report document:
classification normalisation, the Alerts headline, the console Markdown, and the issue
state. `CreateRcaAgentReportRequest` carries exactly one field, `report`, deliberately
unmodelled — mirroring this repo's `RCAReport` in AEP's OpenAPI would have recreated the
same coupling in schema form.

The Go port is asserted against golden files generated by running the **real Python** over
the same reports, so the equivalence was checked against the implementation being replaced
rather than against a second guess at it.

### 14.3 The coupling this actually removed

AEP was not only receiving this report — it was **parsing it back**. `escalate.go` recovered
the recommended actions and their statuses out of the rendered `diagnosis` with an anchored
regex matching, byte for byte, the line this repo's renderer emitted. An undocumented text
format was acting as a cross-repo contract: reordering that line here would have silently
stopped AEP escalating, which is the exact failure escalation exists to prevent (two live
incidents, reports 59796491 and c82f1fb8).

That regex is **deleted**. The decision reads `result.recommendations.recommended_actions[].status`
as fields. AEP's own docstring had asked for this: *"the durable fix is for the SRE agent to
send both as structured fields, which is a contract change on its side."*

One softer dependency remains by choice: the escalated issue still quotes the handoff's
reasoning by slicing the `## Handoff decision` section out of the rendered Markdown
(`handoffReasoning`). It is display-only, it fails safe to an empty quote, and it now reads
Markdown AEP itself renders — so it is no longer a cross-repo contract at all.

### 14.4 Deploy order

**AEP first, then this agent** — the same direction as §16, for the opposite reason: AEP has
to be able to accept the new body before this agent starts sending it. AEP's acceptance
landed additively first, so both shapes worked during the switch; the flat shape has since
been removed, which means an agent older than this change can no longer publish at all. Set
`REPORT_SINK=webhook` and `REPORT_SINK_URL=<aep-api>/api/v1/rca-agent/reports` when rolling
this image out; leaving them unset publishes nowhere and loses the console's Alerts feed
silently.
