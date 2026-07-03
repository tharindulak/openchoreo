# Enabling the RCA (SRE) Agent — Configuration Guide

This guide lists **every configuration** required to run the OpenChoreo RCA agent end‑to‑end,
including the **automatic** alert → RCA flow and making reports visible in the **portal**.
Values shown are for a local **single‑cluster k3d** install; cluster‑specific values (UIDs,
hostnames) are flagged.

> The RCA agent itself is alert‑driven over REST. Nothing auto‑detects a broken deploy — an
> alert must reach the agent's `/analyze` endpoint. The pieces below wire that up automatically
> and surface the results in the portal.

---

## Architecture (automatic flow)

```
ObservabilityAlertRule (incident.enabled + triggerAiRca)
  → logs-adapter / metrics-adapter evaluates the rule
  → alert fires → Alertmanager / adapter
  → observer.HandleAlertWebhook → triggerRCAAnalysis        (gated by AI_RCA_ENABLED)
  → POST http://ai-rca-agent:8080/api/v1alpha1/rca-agent/analyze
  → RCA ReAct loop → structured report
  → remediation agent (REMED_AGENT) appends recommended changes  (does NOT auto-apply)
  → report stored → portal shows it (gated by rcaAgentURL)
```

---

## 1. Deploy the RCA agent (observability plane)

Set in the observability‑plane Helm values (`rca:` block):

| Key | Value | Notes |
|-----|-------|-------|
| `rca.enabled` | `true` | Deploys the `ai-rca-agent` Deployment + Service (`:8080`) |
| `rca.image.repository` / `rca.image.tag` | e.g. `openchoreo-sre-agent` / `anthropic-patched` | **Use an image that supports the chosen model.** The upstream `ghcr.io/openchoreo/sre-agent` may not support Anthropic — a patched image is required for `anthropic:*` models. |
| `rca.modelName` | `anthropic:claude-sonnet-4-6` | LLM used for analysis |
| `rca.remedAgent` | `true` | Enables the experimental remediation agent (recommends only) |
| `rca.secretName` | `rca-agent-secret` | See §2 |

> ⚠️ The k3d install pulls the chart from the **OCI registry** with `--values values-op.yaml`,
> **not** the local `install/helm/openchoreo-observability-plane/values.yaml`. Editing that local
> file has no effect on a k3d install. Set these via the install values override or a manual
> `helm upgrade` against the running release.

Verify:
```bash
kubectl get deploy ai-rca-agent -n openchoreo-observability-plane
kubectl get svc    ai-rca-agent -n openchoreo-observability-plane   # ClusterIP :8080
```

---

## 2. LLM + OAuth secret

The agent loads these via `envFrom` from `rca-agent-secret` (in `openchoreo-observability-plane`):

| Key | Purpose |
|-----|---------|
| `RCA_LLM_API_KEY` | API key for the LLM provider (Anthropic/OpenAI) |
| `OAUTH_CLIENT_SECRET` | Client secret for the agent's OAuth2 client (`openchoreo-rca-agent`) |

```bash
kubectl get secret rca-agent-secret -n openchoreo-observability-plane \
  -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
```
> Treat these as live credentials — keep them out of version control.

Non‑secret config lives in the `rca-agent-config` ConfigMap (model name, `OBSERVER_API_URL`,
`OPENCHOREO_API_URL`, `OAUTH_CLIENT_ID`, `CORS_ALLOWED_ORIGINS`, `REMED_AGENT`).

---

## 3. Observer auto‑trigger config

The **observer** is what calls the agent when an alert fires. Required (in `observer-config`):

| Key | Value | Notes |
|-----|-------|-------|
| `AI_RCA_ENABLED` | `true` | Master switch for auto‑triggering RCA |
| `RCA_SERVICE_URL` | `http://ai-rca-agent:8080` | In‑cluster Service URL the observer POSTs to. **Often missing from installs** — if empty, a fired alert goes nowhere |
| `LOGS_ADAPTER_ENABLED` | `true` | Without this, **log‑based alert rules are never evaluated at all** (see §3b) |
| `ALERT_SUPPRESSION_WINDOW` | `1h` (default) | **De‑dup window — a rule auto‑fires at most once per hour per component** |

```bash
kubectl get cm observer-config -n openchoreo-observability-plane \
  -o jsonpath='{.data.AI_RCA_ENABLED}{"  "}{.data.RCA_SERVICE_URL}{"  "}{.data.LOGS_ADAPTER_ENABLED}{"\n"}'
# expect: true  http://ai-rca-agent:8080  true

# If either is wrong, patch + restart the observer:
kubectl patch cm observer-config -n openchoreo-observability-plane --type=merge \
  -p '{"data":{"LOGS_ADAPTER_ENABLED":"true","RCA_SERVICE_URL":"http://ai-rca-agent:8080"}}'
kubectl rollout restart deploy/observer -n openchoreo-observability-plane
```

---

## 3b. Logs‑adapter — the alert‑evaluation engine (easy to miss entirely)

The observer holds the alert rules (sqlite `ALERT_STORE_BACKEND`) and runs the evaluation
loop, but it executes the log queries through the **logs‑adapter** (`http://logs-adapter:9098`).
No logs‑adapter ⇒ log‑based rules sync fine (`Synced: True`, `Phase: Ready`) but are **never
evaluated** — no alert ever fires, silently.

Key facts (learned by hitting every one of these):

- The logs‑adapter is **NOT part of the `openchoreo-observability-plane` chart** — that chart
  only sets `LOGS_ADAPTER_URL`. The adapter ships in the separate **`observability-logs-opensearch`
  module chart** (`oci://ghcr.io/openchoreo/helm-charts/observability-logs-opensearch`), as
  its `adapter` component (image `ghcr.io/openchoreo/observability-logs-opensearch-adapter`).
- **Chart version matters**: `0.3.x` has no adapter at all. Use `>= 0.5.1`
  (the version the repo's e2e pins — `OBSERVABILITY_LOGS_OPENSEARCH_VERSION` in `make/e2e.mk`).
- Upgrade command that works against an existing install (keep your original values — check
  them with `helm get values observability-logs-opensearch -n openchoreo-observability-plane`):

```bash
helm upgrade observability-logs-opensearch \
  oci://ghcr.io/openchoreo/helm-charts/observability-logs-opensearch \
  --version 0.5.1 -n openchoreo-observability-plane \
  --values <your-values.yaml-including:> \
  # adapter:
  #   openSearchSecretName: opensearch-admin-credentials
  --wait --wait-for-jobs
```

- ⚠️ **Do NOT use `--reuse-values` when crossing chart versions**: it drops the new chart's
  defaults (`adapter.enabled: true`, `adapter.image.repository`, …), so the adapter silently
  renders nothing — or the upgrade fails with
  `nil pointer evaluating interface {}.repository`. Pass a full values file instead.

Verify:
```bash
kubectl get pods,svc -n openchoreo-observability-plane | grep adapter
# expect: pod/logs-adapter-opensearch-… Running  +  service/logs-adapter :9098
kubectl logs -n openchoreo-observability-plane deploy/logs-adapter-opensearch | head
# expect: "Successfully connected to OpenSearch", "Starting server, port 9098"
```

**How evaluation actually works (for debugging):** controller → observer (`/alerts/rules`) →
logs‑adapter → an **OpenSearch alerting‑plugin monitor** (1 per rule, runs on the rule's
`interval`). Inspect the materialised monitors directly:

```bash
kubectl exec -n openchoreo-observability-plane opensearch-master-0 -c opensearch -- \
  curl -sk -u "admin:<opensearch-admin-password>" \
  "https://localhost:9200/_plugins/_alerting/monitors/_search" \
  -H 'Content-Type: application/json' -d '{"query":{"match_all":{}}}'
# expect one monitor per rule, enabled: true, with your interval + a trigger/action
```

### ⚠️ Index‑mapping trap: rule matches 0 docs forever on pre‑existing indices

The adapter's monitor scopes by `term` queries on
`kubernetes.labels.openchoreo_dev/{component,project,environment}-uid`. Those only work when
the fields are mapped **`keyword`** — which the ≥0.5.x module's `container-logs` index template
provides. **Daily indices created before the module upgrade** have the labels dynamically mapped
as `text`, so the term query on the full UID never matches (UIDs tokenise on hyphens) — the
monitor silently evaluates to 0 hits forever. Existing indices cannot be remapped.

Fix for the current day (local/dev — deletes that day's logs, regenerable):
```bash
kubectl exec -n openchoreo-observability-plane opensearch-master-0 -c opensearch -- \
  curl -sk -u "admin:<pw>" -X DELETE "https://localhost:9200/container-logs-$(date -u +%F)"
# fluent-bit recreates the index on the next log line, now with the keyword template
```
Or simply wait for the next daily index (UTC midnight). Confirm with the monitor‑style query
(`term` on the component‑uid + `wildcard` on the log phrase) returning `hits > 0`.

---

## 4. Portal integration — make reports visible

The portal hides RCA ("**AI RCA is not configured**") unless `rcaAgentURL` is set on the
observability‑plane resource that the component's data plane references.

```bash
# Set the gateway-routed URL (same gateway/port as observerURL, agent's host)
kubectl patch clusterobservabilityplane default --type=merge \
  -p '{"spec":{"rcaAgentURL":"http://rca-agent.openchoreo.localhost:11080"}}'

# Verify
kubectl get clusterobservabilityplane default -o jsonpath='{.spec.rcaAgentURL}{"\n"}'

# Force the portal/API to re-read, then hard-reload the browser (Cmd+Shift+R / incognito)
kubectl rollout restart deploy/openchoreo-api deploy/backstage -n openchoreo-control-plane
```

Notes:
- `rcaAgentURL` must follow the same pattern as `observerURL` on the resource
  (`http://observer.openchoreo.localhost:11080` → `http://rca-agent.openchoreo.localhost:11080`).
  The host is confirmed by the agent's `HTTPRoute` (`rca-agent.openchoreo.localhost` → `ai-rca-agent:8080`).
- The component resolves its plane via `ClusterDataPlane.spec.observabilityPlaneRef`
  → `ClusterObservabilityPlane/default`. Patch the plane that ref points to.
- The portal caches the "enabled" flag per session — always **hard‑reload**.

---

## 5. Alert rule — make it actually auto‑trigger

Without an `ObservabilityAlertRule`, nothing ever fires. Create one scoped to the component,
with incident + RCA enabled. **Direct CR** example (log‑based):

```yaml
apiVersion: openchoreo.dev/v1alpha1
kind: ObservabilityAlertRule
metadata:
  name: <component>-log-rca
  namespace: default
  labels:
    # UID labels — REQUIRED for scoping (from the dp Deployment's labels)
    openchoreo.dev/component-uid: <COMPONENT_UID>
    openchoreo.dev/project-uid: <PROJECT_UID>
    openchoreo.dev/environment-uid: <ENVIRONMENT_UID>
    # NAME labels — REQUIRED: observer builds the RCA payload (namespace/project/component/
    # environment) from THESE. Missing them → empty scope → agent 404/500.
    openchoreo.dev/namespace: default
    openchoreo.dev/project: default
    openchoreo.dev/component: <component>
    openchoreo.dev/environment: development
spec:
  name: <component>-log-rca
  description: "Auto-RCA on matching log lines"
  severity: critical
  enabled: true
  source:
    type: log                 # log | metric | budget
    query: "<phrase>"         # metric alerts only support cpu_usage / memory_usage
  condition:
    window: 5m
    interval: 1m
    operator: gte             # gt | lt | gte | lte | eq
    threshold: 1              # MUST be > 0
  actions:
    notifications:
      channels: [ default ]   # REQUIRED: at least 1 item (placeholder ok if no real channel)
    incident:
      enabled: true           # REQUIRED for triggerAiRca
      triggerAiRca: true      # the switch that calls the RCA agent
```

Get the UIDs from the running deployment:
```bash
kubectl get deploy <dp-deploy> -n dp-<...> -o jsonpath='{.metadata.labels}' | tr ',' '\n' | grep uid
```

Apply and confirm it synced to the observer backend:
```bash
kubectl apply -f alert-rule.yaml
kubectl get observabilityalertrule -n default \
  -o custom-columns=NAME:.metadata.name,SYNCED:'.status.conditions[?(@.type=="Synced")].status'
# SYNCED must be True
```

---

## 6. Verify the full automatic flow

```bash
# Watch the IN-CLUSTER agent (NOT /tmp/sre-agent.log — that's the local dev copy)
kubectl logs -f -n openchoreo-observability-plane deploy/ai-rca-agent | grep -vE "Pydantic V1"
```

Trigger the matching condition (for the log example, emit the phrase). Within ~1–2 min you
should see, in order:
1. `POST /api/v1alpha1/rca-agent/analyze … 200 OK`
2. `RCA completed: usage={'claude-sonnet-4-6': …}`
3. `Running remediation agent` → `Remediation completed`
4. `Updated RCA report to completed`

Then hard‑reload the portal → the report appears for the component.

---

## Gotchas (learned the hard way)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Portal: "AI RCA is not configured" | `rcaAgentURL` unset on the plane resource | §4 |
| Auto‑trigger never fires | No `ObservabilityAlertRule`, or `AI_RCA_ENABLED` false, or rule missing `incident.enabled`/`triggerAiRca` | §3, §5 |
| Rule is `Ready`/`Synced: True` but **never fires**, even with matching logs in the pod | **No logs‑adapter deployed** (module chart 0.3.x has none) and/or `LOGS_ADAPTER_ENABLED=false` — rules are stored but never evaluated | §3b |
| Alert fires but agent never receives `/analyze` | `RCA_SERVICE_URL` empty in `observer-config` | §3 |
| `helm upgrade` of the logs module fails with `nil pointer …image.repository`, or adapter silently absent after upgrade | `--reuse-values` across chart versions drops new defaults (`adapter.enabled`, `adapter.image.*`) | §3b — pass a full values file |
| Rule stops firing after an **observer restart** | Observer's alert store is **ephemeral sqlite** (`ALERT_STORE_BACKEND=sqlite`) — a restart wipes it, and the controller won't re‑push (`observedGeneration` unchanged ⇒ "unchanged in backend"). Annotations do NOT trigger re‑sync | Force a spec change on each rule, e.g. `kubectl patch observabilityalertrule <rule> -n default --type=merge -p '{"spec":{"description":"…resynced"}}'` — expect `Synced` message to flip to "updated in backend" |
| Agent returns **500**, URL `/namespaces//projects/` | Rule missing the **name** labels → empty scope | Add `openchoreo.dev/{namespace,project,component,environment}` labels (§5) |
| Rule rejected: `threshold must be greater than zero` | `threshold: 0` | Use `operator: gte, threshold: 1` |
| Rule rejected: `notifications.channels … at least 1 item` | empty `channels` | Add ≥1 channel (placeholder OK) |
| Second trigger does nothing within the hour | `ALERT_SUPPRESSION_WINDOW=1h` per rule+component | Rotate the rule name per demo run |
| Metric alert won't model a broken image | metric source only supports `cpu_usage`/`memory_usage` | Use a `log` (or `metric`/`budget`) source |
| Watching `/tmp/sre-agent.log` shows nothing on auto‑runs | That's the **local** `:8000` dev agent; auto path hits the **in‑cluster** `ai-rca-agent` | Tail the pod logs |

## 7. Optional: AE coding‑agent handoff (`AE_HANDOFF`)

With the handoff feature enabled, an RCA whose recommendations include a **code‑level** fix
additionally files a GitHub issue via the Agentic Engineer platform and (with
`AE_AUTO_DISPATCH=true`) dispatches the AE coding agent against it. Setup, auth
accommodation (`JWT_AUDIENCE`), the component‑naming rule, and the duplicate‑dispatch race
warning (set `ALERT_SUPPRESSION_WINDOW`!) are documented in **`AE-HANDOFF-DESIGN.md` §11/§14**.
Verified E2E on this stack 2026‑07‑03: alert → RCA → issue → coding‑agent PR flow.

## Important behavior note

The remediation agent **reviews and revises** RCA recommendations into concrete ReleaseBinding
changes — it does **not** apply them (its prompt: *"do not execute or apply any actions"*).
Application is manual (a human marks actions applied via the portal). Do not describe this as
auto‑remediation/self‑healing.
