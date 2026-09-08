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
2. ~~**Auto-dispatching** the AE coding agent against that issue.~~

**REVISED (2026-09-08):** filing IS the hand-over. There is no dispatch step, and no
dispatch switch on this side: `ae_create_issue` files and adopts in one call, and whether a
coding run actually starts is the RECEIVER's answer on that call (`adopted`,
`adoptionError`, `suppressed`) — read back and recorded, never decided here.

~~The AE coding agent then finds and cross-links **related existing issues** (a capability the
SRE agent lacks today, because it never looks at the code repo) and opens a PR.~~ Cross-linking
moved to this side in 2026-07-06 (decision #7 below): the issue arrives pre-linked, and the
coding agent opens a PR.

**Correction (2026-08-19):** this document originally said "the PR is the human review gate —
the coding agent never merges" and "No auto-merge. The coding agent stops at 'PR opened'". The
first half is true and the second was misleading: the *agent* never merges, but AE's **merge
policy does** (`eventcore/merge.go` squash-merges a pull request that resolves its run's
milestone work), so an incident fix reached production with no human in it. AEP's ADR-0018 adds
the human gate this text assumed — see §11.

### Non-goals
- The RCA agent's telemetry investigation and scope-enforcement rules are **unchanged**. The
  handoff is a new downstream stage, exactly like the remediation agent was added.

---

## 2. Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Where the "skill" logic lives | ~~**Both sides** — SRE prompt+tools decide/create the issue and dispatch; AE-side `SKILL.md` does related-issue discovery/commenting during coding-agent execution~~ **REVISED (2026-09-08): the receiver's side, in one skill it mounts into this agent.** How to search, what the issue must say and what each answer means is `coding-agent-handoff/SKILL.md` — owned by AEP, mounted at deploy time (B6). `handoff_agent_prompt.j2` is a LOADER: the run-time scope values plus the skill catalog, and no doctrine. The code-vs-config decision is in neither — it is derived in code (§8) |
| 2 | Shape of the "AE MCP" | **New thin TypeScript MCP server in the AE repo**, wrapping `aep-api` REST. SRE agent stays a pure MCP client |
| 3 | SRE → AE MCP auth | AE MCP accepts the **same service-account OAuth2 bearer** the RCA path already uses (`get_oauth2_auth()`) |
| 4 | Cross-system trust | **Shared / federated IdP** — the SRE service-account token carries an `ouHandle` claim that AE's `aep-api` trusts. No separate publisher client-credentials principal |
| 5 | OC → AE project/repo mapping | **AE resolves it** — SRE passes OC project/component; a new `aep-api` lookup resolves the AE project + GitHub repo |
| 6 | Handoff behavior | ~~**Auto-dispatch** — create issue → dispatch coding agent. Flag-gated (default off), stops at PR-opened, recorded on the RCA report~~ **REVISED (2026-09-08): one call, and adoption is the receiver's answer.** `HANDOFF_ENABLED` gates the stage (default off); there is no `AE_AUTO_DISPATCH`. Filing adopts, the answer says whether anything will work the issue, and that answer is recorded on the RCA report. Where a run stops is AEP's policy (§11.2), not a guarantee this repo makes |
| 7 | Related-issue commenting | ~~AE-side `SKILL.md` in the coding-agent pod~~ **REVISED (2026-07-06): moved to the SRE-side handoff agent, body-mentions only.** It reads `ae_search_related_issues` results (full bodies) and writes a `## Related issues` section (`- #N — reason`) into the issue body it creates; GitHub's automatic cross-reference events on `#N` mentions provide the back-links, so no comments are posted on other issues at all. The AE-side `related-issues` skill, its `skillPreload` entry, and the `buildAgentPrompt` instruction are removed — issues arrive at the coding agent pre-linked |

---

## 3. Key facts that shaped this design

*Point-in-time facts from when this was designed (2026-07), kept because they explain the
shape. Current behaviour is §4 onward; where one of these has since changed it says so.*

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
  *(Since retired: adoption folded into the create call — AEP's ADR-0017 — so there is one
  call and no dispatch endpoint.)*
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
        └─► classification  DERIVED from remediation's statuses, in code, before
              any model runs (HandoffClassification.derive, §8). code_level or
              mixed ⇒ the stage runs; config_level or none ⇒ it does not
        └─► HANDOFF_AGENT  (HANDOFF_ENABLED flag, RootCauseIdentified only)
              1. load_skill('coding-agent-handoff')  ── the playbook, mounted by
                                                        the receiver (B6)
              2. <search tool>(...)     ── related-issue discovery, for links
              3. <create tool>(...)     ── ONE issue: RCA context in a fixed
                                           heading skeleton. Identity (project,
                                           component, error signature) rides
                                           per-run HEADERS, so dedupe is the
                                           receiver's to derive, not the model's
                                           to spell
              4. record what the answer said (issue number/url, deduped, and the
                 descriptor's `facts`: adopted, adoptionError, reopened,
                 recurrence, suppressed) on the RCA report
                        │
   ┌────────────────────┘  (crosses into the AE repo)
   ▼
AE aep-mcp-server → aep-api  →  files the issue into the deployed version's
               milestone as agent work  →  starts (or wakes) the milestone's
               coding run
   ▼
AE coding-agent pod (remote-worker, Claude Agent SDK)
   ├─ reads the issue body's heading skeleton (already carrying the RCA context
   │  and the `- #N` cross-links this side wrote)
   ├─ implement code fix — or close as not planned, with reasons (ADR-0023)
   └─ gh pr create "Closes #<n>", declaring Confidence (§11.2 — low or missing
      holds the PR for a human)
```

---

## 5. The receiving side (AEP) — what this repo depends on, and nothing more

**REVISED (2026-09-08).** This section used to specify AEP's REST endpoints, request shapes
and skill wiring (A1-A3), which is a contract this repo neither owns nor can test — and whose
every change became a change here. It is now a pointer, deliberately: what AEP exposes and how
is AEP's to document.

This agent depends on exactly four things from a receiver, all of them named by the receiver
itself:

1. **An MCP endpoint** carrying two tools — one that searches existing issues, one that
   creates an issue and hands it over. Reached at `HANDOFF_API_URL` + `HANDOFF_MCP_PATH`
   (today: AEP's standalone `services/aep-mcp-server`, `:3401` + `/mcp`).
2. **A provider descriptor** giving that receiver's names for those tools, for the per-run
   identity headers and for the answer fields worth recording (B6).
3. **A skill**, mounted into this agent at deploy time, carrying the playbook for writing the
   issue (B6).
4. **An answer** on the create call that says what happened — filed, deduped, reopened,
   suppressed, adopted or not.

Everything else — how the dedupe key is derived, which milestone the issue is filed into, what
label arms a coding run, whether a PR needs a human — is the receiver's, and this repo holds no
copy of it.

AEP's own `services/aep-mcp-server/skills/README.md` and its API docs are the authority for the
current shapes; ADR-0017, ADR-0018, ADR-0021 and ADR-0023 in
`labs-agentic-engineer/docs/decisions/` carry the decisions behind them.

One receiver-side boundary is worth stating here anyway, because it decides whether a handoff
can succeed at all: AEP resolves the component against **its own design** before filing
anything (`ensureNamedComponent`, §12.1), so the handoff only reaches components AEP itself
created. Any other component fails the create call with a 400 naming the string that was sent —
before an issue exists — and nothing retries a handoff.

---

## 6. Changes — Repo B: `openchoreo/agents/sre-agent` (this repo)

### B1. Config — `src/config.py`
As shipped (the `AE_*` names above were retired with AEP's vocabulary):
```python
handoff_enabled: bool = False       # gate the whole stage (mirrors remed_agent)
handoff_api_url: str = ""           # the receiver's base, e.g. http://aep-mcp-server:3401
handoff_mcp_path: str = "/mcp"      # its MCP endpoint under that base
handoff_provider_file: str = ""     # the receiver's descriptor (B6)
external_skills_dir: str = ""       # where its mounted skill is read from (B6)

@property
def handoff_mcp_url(self) -> str:
    return f"{self.handoff_api_url.rstrip('/')}/{self.handoff_mcp_path.strip('/')}"
```
`_validate_handoff_config` raises at STARTUP when `handoff_enabled` is set without
`handoff_api_url` or `handoff_provider_file`. Config fails quietly otherwise: a missing
descriptor would not crash anything, it would file a duplicate issue for every recurrence
until a human noticed.

### B2. MCP client — `src/clients/mcp.py`
A third server, keyed `handoff` (only when `handoff_enabled`), reusing the same `auth` object
already passed in — plus this run's identity as headers:
```python
connections["handoff"] = {
    **base,                              # transport, httpx factory, auth
    "url": settings.handoff_mcp_url,
    "headers": handoff_headers,          # per-run identity, from the descriptor (B6)
}
```
The headers are built by the stage from the ALERT's own scope and error fingerprint
(`provider.incident_headers(...)`), never by the model: a sibling component the model chose
would be a valid name filed under the wrong dedupe namespace.

### B3. Tool registry — `src/agent/tool_registry.py`
A `HANDOFF = "handoff"` server constant and `HANDOFF_ACTIVE_FORMS` (what the UI shows while a
handoff tool runs). **The tool NAMES are deliberately absent** from the registry: they belong
to whichever platform receives the handoff and arrive in its descriptor (B6), so this module
cannot know them at import. `Agent.create()` splits them into the template context as
`handoff_tools`, read off the descriptor at request time.

### B4. The agent stage — `src/agent/agent.py` + `src/templates/prompts/handoff_agent_prompt.j2`
As shipped:
```python
HANDOFF_AGENT = Agent(
    template="prompts/handoff_agent_prompt.j2",
    tools=lambda: load_provider().tools,   # callable: the RECEIVER names its tools (B6)
    middleware=[LoggingMiddleware, ToolErrorHandlerMiddleware],
    response_format=HandoffSummary,        # a summary, not a decision (B5)
    recursion_limit=50,
    skills={"coding-agent-handoff"},       # mounted by the receiver (B6)
)
```
Wired into `run_analysis()` after the remediation block, guarded by `settings.handoff_enabled`,
`isinstance(rca_report.result, RootCauseIdentified)` and `classification.needs_stage` (§8).
Wrapped in try/except like remediation, so a handoff failure never fails the RCA report. The
user turn is the report itself — `handoff_view(report_data)` (§8) — and carries no
instructions.

**The prompt is a LOADER.** It carries the run-time scope values, one sentence telling the model
to call `load_skill` first, and the skill catalog. It encodes no decision (§8 derives it), names
no receiving platform, and holds no doctrine: how to search, what the issue must say, and what
each answer means all live in the mounted skill, which the receiver ships and can change without
touching this repo. Anything receiver-specific added back here is a contract this repo cannot
test — including in the model-visible field descriptions of B5.

`_wrap_create_issue` is **gone**, with the flag it served. It forced `adopt`, `dedupeKey`, the
`sre-agent` label and a de-prefixed `componentName` onto every call; adoption and the labels are
now the receiver's (§11, §12.3), the dedupe key is derived server-side from the B2 headers, and
`componentName` is the model's to pass — the one argument the skill makes it responsible for.

### B5. Report model — `src/models/handoff_result.py`
Two models, because two different things are recorded — what the model wrote, and what the
wire said:
```python
class HandoffSummary(BaseModel):      # the LLM's response_format
    rationale: str                    # what was filed, or what the answer means
    related_issues: list[RelatedIssue] = []

class HandoffResult(BaseModel):       # the persisted record
    classification: HandoffClassification   # DERIVED (§8), never restated
    rationale: str
    related_issues: list[RelatedIssue] = []
    deduped: bool = False                   # first-class: the agent ACTS on it
    created_issue_number: int | None = None
    created_issue_url: str | None = None
    provider_facts: dict[str, Any] = {}     # the descriptor's `facts`, verbatim
```
`deduped` is the one answer with a typed home of its own, because it is the one this agent
acts on: a deduped handoff is not published downstream, since the run that created the issue
already reported this incident (§13.1).
`HandoffSummary` **carries no decision.** Whether code-level work exists follows from
remediation's statuses (§8), and whether a fix is warranted is the coding agent's call, made
with the repository and the spec in front of it. A model asked to restate either would be in a
position to contradict the data it was handed — which is what happened.

The issue fields and `deduped` are STAMPED from the create call's answer by
`HandoffOutcomeMiddleware` (which replaced `apply_handoff_facts`), keyed by the descriptor's
answer names. Everything else the receiver said lands in `provider_facts` untyped and
uninterpreted — `adopted`, `adoptionError`, `reopened`, `recurrence`, `suppressed` today. That
is deliberate: a fact this agent cannot verify is a fact it must not paraphrase, and typed homes
here meant the record could only ever describe one receiver. The console's Alerts list serves
this snapshot as-is, so a loose restatement would show a human the wrong state while they triage.

Attached as `RCAReport.handoff: HandoffResult | None`, persisted via `upsert_rca_report`.
Model-visible field descriptions here name no receiver either (B4).

### B6. What the receiver mounts — descriptor + skill

Both arrive as deploy-time config, and the stage refuses to start without the descriptor (B1):

- **The descriptor** (`HANDOFF_PROVIDER_FILE`, read by `src/agent/handoff_provider.py` — the
  only module that reads it) names the receiver's two tools, the three per-run identity headers
  (B2), and the answer fields: `issue_number`, `issue_url`, `already_filed`, plus a `facts` list
  carried through verbatim (B5). It RENAMES things; it never changes what identity is sent, what
  is recorded, or what the handoff concludes. A receiver needing different BEHAVIOUR is not a
  descriptor change.
- **The skill** (`EXTERNAL_SKILLS_DIR`, read by `src/agent/skills.py`) is the stage's whole
  playbook. `src/skills` is the built-in library and is searched second, so a mounted skill
  overrides or adds to it; `coding-agent-handoff` has no built-in copy at all, which means an
  unmounted skill fails agent creation rather than degrading into a run with no instructions.
  The catalog (name + description) is always in the prompt; the BODY loads on demand through the
  `load_skill` tool — progressive disclosure in a framework that has none natively.

AEP ships both from `services/aep-mcp-server/` — `handoff/provider.json` and
`skills/coding-agent-handoff/` —
rendered into ConfigMaps and mounted by its `setup-observability.sh` — no image rebuild for a
change to either. See §13.4 and §12.4 for deploy order.

---

## 7. What the two handoff tools are for

**REVISED (2026-09-08).** The input/output tables that were here specified AEP's tool schemas.
Those are the receiver's, they are already stated in the descriptor and the mounted skill (B6),
and a second copy in this repo could only drift. The ROLES are stable, and they are all this
repo relies on:

| Role | Descriptor key | What this agent needs from it |
|---|---|---|
| search existing issues | `search_related` | keyword search over the project's issues, so the issue this stage files can carry `- #N` cross-links |
| create + hand over | `create_issue` | ONE call that files the issue and hands it over, answering with the issue number/url, whether it was already filed, and the receiver's own facts |

Three properties this agent does depend on:

- **Creating the issue IS the hand-over.** There is no dispatch tool and no second call. Whether
  a coding run starts comes back in the answer.
- **Identity is not an argument.** The project, component and error signature ride as per-run
  headers (B2), so the dedupe key is derived by the receiver from a value the model cannot
  spell. `componentName` on the create call is the exception, and the skill makes the model
  responsible for it.
- **The answer is authoritative.** `deduped`, `reopened` + `recurrence`, `suppressed`,
  `adopted` + `adoptionError` each mean something different for the incident, and the stage
  reports the consequence rather than re-deriving it (B5, §11).

`project` is the OpenChoreo project name; the receiver resolves its own repo and credentials.
The component name is sent verbatim as OpenChoreo spells it — de-prefixing belongs to the side
that owns the design (§12.1).

---

## 8. Config-vs-code decision logic

**Derived in code from the remediation output** — `HandoffClassification.derive`
(`src/models/handoff_result.py`) — before any model runs. No prompt encodes it and no model
input reaches it:

- **Config-level** (no issue): actionable as an OC ReleaseBinding change — i.e. remediation
  produced a `RemediationAction` with `status == "revised"` and a concrete `ResourceChange`
  (env var, replica count, resource limits, file-mount content, trait/componentType override).
- **Code-level** (→ one issue): requires a source change — e.g. add/adjust structured
  logging, fix a null deref, add a timeout/retry, correct business logic. Typically surfaces as
  a `status == "suggested"` action with no `change`, or an RCA root cause that no config knob
  can address.
- **Mixed:** apply config via the existing OC path AND file a code issue for the code part.

**The model is asked nothing about this.** `derive` reads the statuses: an action whose status
is not settled is PENDING work, `revised` means configuration already expressed it, and an
ABSENT status means remediation never ran — which is pending, not settled, so the root cause
is handed over on its own terms. `code_level` (pending, nothing revised) and `mixed` (pending,
something revised) run the stage; `config_level` and `none` do not (`needs_stage`).

This replaced asking the model for a `needs_code_change` boolean, which is what failed on two
live reports: a stage handed the remediation agent's verdict can only disagree with it. The
classification is therefore not in `HandoffSummary` at all (B5).

Two things are withheld from the payload the model sees (`handoff_input`), both boundaries rather
than instructions. `observability_recommendations` goes entirely — advice for making future
analyses easier is not this incident's fix, and it is the main source of issues a coding agent
cannot act on. And the `change` patch is stripped from every already-`revised` action: the issue
becomes a coding agent's prompt, that agent can only edit the repository, and a concrete
ReleaseBinding patch in front of it invites a pull request expressing config as code. The
action's description and status stay, so the issue can say "this part was already fixed by
configuration" instead of asking for it again.

---

## 9. Auth & identity

Decision #4 = shared/federated IdP. **Confirmed empirically on the live run (2026-07-03):** the
agent's `openchoreo-rca-agent` client-credentials token (k3d Thunder) carries
`iss=http://thunder.openchoreo.localhost:8080` — matching `aep-api`'s `JWT_ISSUER` — and
`ouHandle: default`, the org claim AEP's tenant gate needs. The one accommodation was audience:
the token's `aud=openchoreo-rca-agent` does not match `aep-*`, so AEP's `JWT_AUDIENCE` is a
comma-list (`aep-*,openchoreo-rca-agent`). No separate publisher principal was needed.

Concretely it requires:
- OC's IdP (whatever issues the SRE service-account token via `get_oauth2_auth()`) and AE's
  Thunder to be the **same or federated**, so `aep-api` can verify the token against its
  configured issuer/JWKS/audience.
- The SRE **service account** to carry an `ouHandle` (org) claim identifying the AE org.

Beyond the token, this run's IDENTITY travels as headers rather than tool arguments: the
descriptor names them (`incident_headers`) and `MCPClient` attaches them to the handoff
connection for the run (B2, B6). A value the model cannot spell is a value it cannot get
wrong — and the dedupe key derived from them is the receiver's, not this agent's.

---

## 10. Safety & WSO2 org-policy alignment

- **The stage is flag-gated** (`HANDOFF_ENABLED`, default off) and refuses to start without a
  provider descriptor and a handoff URL (`_validate_handoff_config`), so the behaviour is
  opt-in per deployment and cannot half-start.
- ~~**No auto-merge** — the coding agent opens a PR only; human review/merge is the gate.~~
  **CORRECTED (2026-08-19, restated 2026-09-08):** this repo does not provide that control and
  never did. AE's merge policy squash-merges a PR that resolves its run's milestone work, so
  the gate had to be built: AEP's **ADR-0018** holds any low-confidence or unparsed-confidence
  incident PR for a human (§11.2). The gate is real, it is AEP's, and it is documented there —
  not implied by anything here. A deployment whose receiver has no such policy has no human in
  that path, and this agent cannot tell.
- **Least privilege** — new MCP tools are the only write surface added to the SRE agent; GitHub
  credentials stay in AE (the SRE agent never holds them). Matches the "tools are an allow-list"
  principle in `EXTENDING.md`.
- **Auditability** — what the receiver ANSWERED is recorded on the RCA report (B5), stamped
  from the wire by `HandoffOutcomeMiddleware` rather than restated by the model: issue number
  and url, `deduped`, and every field the descriptor lists under `facts`.
- **One write, and only one** — the stage's whole side effect is a single create-issue call
  (`HandoffSummary` + the skill's one-issue rule). It holds no merge, deploy or edit surface.
- **Secrets** — the SRE token is forwarded, not logged; MCP server masks bearer in logs.

---

## 11. Recurrence and the confidence gate (2026-08-19)

Two changes on the AE side, both recorded in AEP's **ADR-0018 — "a merged fix is not a
resolved incident"**. Nothing in this repo's contract changed: the handoff still makes ONE
create-issue call. ~~with the same forced `dedupeKey`, `componentName`, `adopt` and `sre-agent`
label.~~ **REVISED (2026-09-08):** none of those are forced here any more — the key is derived
by the receiver from the per-run identity headers, adoption and the labels are the receiver's,
and `componentName` is the model's to pass (B4, §12.3). One call is still the whole write.

### 11.1 A recurrence reopens

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

**What this repo consumes:** the create call answers with `reopened: true` and `recurrence`
(which attempt this is). Both are listed in the descriptor's `facts` and land in
`HandoffResult.provider_facts` verbatim, stamped from the wire by `HandoffOutcomeMiddleware`
(which replaced `apply_handoff_facts`) and never restated by the model — like `adopted`,
`adoptionError` and `suppressed`. `recurrence` is forwarded on the RCA report so the console can
show "Attempt 3". The handoff never decides a recurrence; AE does, deterministically. What the
model does with the answer is the mounted skill's rule: a failed fix that came back is a
different situation from a new bug, and its `rationale` has to say so.

The loop **never gives up**: past attempt 3 a recurrence is escalated, which means it is
reported loudly and worked anyway.

### 11.2 Incident fixes declare their confidence

AE's coding agent now writes `Confidence: high|low` into the pull request body of any PR
resolving an `sre-agent` issue. `high` merges as before; `low`, missing or unparseable **holds**
the PR for a human, and the run parks without spending re-dispatch budget. Spec builds are
unaffected.

This is the human gate §1 originally (wrongly) claimed already existed. It lives entirely in
AEP; nothing in the SRE agent needs to know about it, beyond the fact that a filed incident may
now take longer to reach production because a person is in the path.

---

## 12. AE's conventions moved to AE (2026-08-31)

Two things this repo enforced were facts about AE's own model, re-derived here from
conventions it does not own. Both now live on the AEP side, and `handoff_logic.py` shrank by
exactly the amount that was never its business — then went away entirely (§12.3, B4): what
remained of it was arguments this repo pinned onto the create call, and those are the
receiver's.

### 12.1 The component name is resolved by the side that owns the design

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

~~`_wrap_create_issue` still FORCES `componentName`.~~ **REVISED (2026-09-08):** the wrapper is
gone, and with it the last argument this repo pinned onto the create call. The concern it served
— the model answering with whatever name it had just read out of a related issue — is now met
upstream of the model entirely: the incident's project, component and error signature ride as
per-run HEADERS (B2), so the dedupe key is derived from values the model cannot spell.
`componentName` on the call is the model's to pass, and the mounted skill makes it the one
argument it is responsible for getting right. The OpenChoreo name still goes verbatim.

### 12.2 AE's planned-work issues arrive already marked

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

### 12.3 The tracking label moved too (2026-09-08)

~~The `sre-agent` label is still applied here, in `_wrap_create_issue`. Moving it needs AE to
identify the CALLER, and aep-api's verified `Claims` carries no audience — only `Subject`,
`ClientID` and the org claims — so a server-side stamp would assert provenance it cannot check,
which is no better than the caller self-reporting it and worse for being invisible. It moves when
there is a verified caller identity to key it off, not before.~~

**REVISED:** it moved with the rest of the create call's derived arguments. AEP now adds `bug`
and `sre-agent` to every issue filed through the handoff surface, on top of whatever `labels`
the caller passes, and the mounted skill states that as settled. The provenance objection was
answered by the same change that removed the forced arguments: the incident identity arrives as
headers on a connection AEP authenticates, so it is deriving the label from a request it has
verified rather than from a string the caller self-reported.

The label stays load-bearing — it gates `eventcore/unverified.go` and ADR-0018's
reopen-on-recurrence, and it is how a human filters for every issue this system has ever filed,
independent of the per-component dedupe key. What changed is who applies it.

### 12.4 Deploy order

aep-api must be deployed **before** this agent. The removals here make the agent send an
OpenChoreo-prefixed `componentName` and stop annotating search results; an aep-api that predates
`ensureNamedComponent` refuses that name with a 400 and files nothing — and nothing retries a
handoff. The reverse order is safe: the old agent's already-stripped name resolves as-given under
the new aep-api, and its own annotation is simply redundant with the server's.

---

## 13. Report publishing became a generic sink (2026-08-31)

`src/clients/aep_reports.py` is **deleted** — all 275 lines. It was the last piece of
this repo that spoke AEP's contract: it spelled `/api/v1/rca-agent/reports`, mapped
AEP's request field names, translated the classification enum's underscores to AEP's
hyphens, and rendered the Markdown layout of AEP's console. Carrying that in an
Apache-2.0 upstream project made every AEP contract change a change to somebody else's
repository, and `agents/common/` arriving upstream (PR #4372) made the generic/specific
line explicit enough that it could no longer be ignored.

### 13.1 What this repo has now

`src/clients/sink/` — `ReportSink`, one abstract method, plus `get_report_sink()` keyed on
`REPORT_SINK`. `WebhookReportSink` POSTs `{"report": <the agent's own report>}` to
`REPORT_SINK_URL` with the same OAuth2 service-account credentials the handoff already
uses. It maps nothing. Config: `AE_PUBLISH_REPORTS` and `AEP_API_URL` are gone, replaced by
`REPORT_SINK` / `REPORT_SINK_URL`.

`should_publish_report` lives in `src/clients/sink/report_sink.py` (it was in
`handoff_logic.py`, which is gone). It is one rule — a handoff that **deduped** folded this
incident onto an issue an earlier run already filed, and that run already published its own
report; a second row would read as unworked while a coding agent is on it. It reads the handoff
result rather than anything provider-shaped, which is what leaves the sink provider-neutral.

### 13.2 What AEP has now

`internal/ops/createreport/native.go` derives every column from the report document:
classification normalisation, the Alerts headline, the console Markdown, and the issue
state. `CreateRcaAgentReportRequest` carries exactly one field, `report`, deliberately
unmodelled — mirroring this repo's `RCAReport` in AEP's OpenAPI would have recreated the
same coupling in schema form.

The Go port is asserted against golden files generated by running the **real Python** over
the same reports, so the equivalence was checked against the implementation being replaced
rather than against a second guess at it.

### 13.3 What the coupling actually removed

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

### 13.4 Deploy order

**AEP first, then this agent** — the same direction as §12, for the opposite reason: AEP has
to be able to accept the new body before this agent starts sending it. AEP's acceptance
landed additively first, so both shapes worked during the switch; the flat shape has since
been removed, which means an agent older than this change can no longer publish at all. Set
`REPORT_SINK=webhook` and `REPORT_SINK_URL=<aep-api>/api/v1/rca-agent/reports` when rolling
this image out; leaving them unset publishes nowhere and loses the console's Alerts feed
silently.
