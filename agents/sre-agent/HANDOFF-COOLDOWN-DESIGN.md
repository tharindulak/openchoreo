# Design: Handoff Cooldown Gate

**Status:** Proposed
**Repo touched:** `openchoreo/agents/sre-agent` (this repo) only — no change to AE
**Author:** tharindulak (with Claude Code)

---

## 1. Problem

A flapping alert — the same underlying error, re-firing repeatedly while the
condition holds — triggers a full RCA → remediation → handoff pipeline run on
every firing, with no suppression anywhere in this repo. Each run's handoff
stage calls AE's handoff MCP server (search existing issues, then create),
which in turn calls the GitHub API. AE already dedupes those calls onto one
open issue (`AE-HANDOFF-DESIGN.md` §8, §9, §10.1), but only *after* paying for
the round-trip — repeated firings of one incident still cost one GitHub-facing
call each, with no upper bound on frequency. Enough of them in a short window
risks rate limiting or throttling on AE's side, which this repo has no
visibility into and no way to recover from once it happens.

### Non-goals

- No change to RCA or remediation — both run in full on every request,
  unaffected by anything in this design.
- No change to AE's own dedupe/reopen/classification logic
  (`AE-HANDOFF-DESIGN.md` §8, §9, §10.1) — this is a caller-side throttle in
  front of a receiver that already dedupes; it does not replace that logic or
  assume it away.
- No batching or queuing of alerts. Each request is still handled
  independently; this only decides whether *its* handoff call proceeds.
- No coarse, pre-RCA suppression. Considered and rejected — see §6.

---

## 2. Where the gate lives

`src/agent/agent.py`, inside `run_analysis()`, immediately before
`HANDOFF_AGENT.create()` is invoked (today at line ~501) and right after the
fingerprint is computed (today at line ~517):

```python
if settings.handoff_enabled and isinstance(rca_report.result, RootCauseIdentified):
    ...
    fingerprint = error_fingerprint(report_data, raw_log_lines)
    dedupe_key = f"{scope.project}/{scope.component}/{fingerprint or 'nofp'}"

    if not await report_backend.try_acquire_handoff_slot(
        dedupe_key, settings.handoff_cooldown_seconds
    ):
        logger.info("Handoff suppressed: cooldown active for dedupe key %s", dedupe_key)
    else:
        try:
            ... # existing handoff block, unchanged
        except Exception as e:
            ...
```

This is the one point both entry points converge on:

- The alert-driven `POST /api/v1alpha1/rca-agent/analyze` route
  (`src/api/agent_routes.py`).
- The manual, on-demand `analyze_runtime_state` MCP tool
  (`src/mcp_server.py`), which the OpenChoreo portal's chat assistant calls
  on a human's behalf to trigger a fresh analysis outside of any alert.

Both call `run_analysis()`, so putting the gate here means **one code path,
no special-casing per trigger source** — a manual trigger is subject to the
exact same cooldown as an automated one. This was a deliberate choice (see
§7): the RCA/diagnosis half of a manual trigger always reruns regardless of
the gate, so the human still gets a fresh answer; only the redundant
handoff/GitHub call is what the gate would additionally suppress, and if an
automated firing already filed or deduped an issue for the same fingerprint
minutes earlier, a second handoff call would just dedupe onto it again
anyway.

---

## 3. Dedupe key

```
f"{scope.project}/{scope.component}/{fingerprint or 'nofp'}"
```

Reuses `error_fingerprint()` (`src/agent/fingerprint.py`) exactly as it
exists today — no changes to that module. Falls back to the alert's own
source signature, then to `"nofp"`, matching the existing fallback chain
that function already implements for `None` handoff-header cases.

`project` is included even though AE's own dedupe key is component-only
(`AE-HANDOFF-DESIGN.md` §9: `sre-rca/<component>/<fingerprint>`). This makes
the local cooldown strictly *more* specific than AE's — two same-named
components in different projects never share a cooldown slot. Worst case,
this repo calls handoff slightly more often than AE's own key would strictly
require; it never suppresses something AE would have treated as a distinct
incident.

The cooldown is per-key, not global: a different fingerprint, a different
component, or a different project each get an independent cooldown clock. An
unrelated incident is never held back by another one cooling down.

Accepted risk: when `error_fingerprint` returns `None` (no fingerprintable
log lines or alert signature), the key degrades to `project/component/nofp`,
so two genuinely distinct incidents on the same component that both yield no
fingerprint would share one cooldown slot. This is accepted as low-risk — it
is bounded to a delay of at most `cooldown_seconds`, not indefinite
suppression, and requires the rare case of zero fingerprintable data — rather
than something being fixed now.

---

## 4. Persistence and concurrency

One new table alongside `rca_reports` in the existing SQL backend
(`src/clients/backend/sql_backend.py` — sqlite and postgresql both already
supported, no new infrastructure):

```python
handoff_cooldowns = Table(
    "handoff_cooldowns",
    metadata,
    Column("dedupe_key", String, primary_key=True),
    Column("last_handoff_at", String, nullable=False),  # ISO-8601, UTC
)
```

One new method on the `ReportBackend` ABC
(`src/clients/backend/report_backend.py`):

```python
@abstractmethod
async def try_acquire_handoff_slot(
    self, dedupe_key: str, cooldown_seconds: int
) -> bool:
    """Atomically check-and-stamp. Returns True (caller may proceed) only if
    no prior stamp exists for dedupe_key, or the prior stamp is older than
    cooldown_seconds. Stamps 'now' in the same statement, so two concurrent
    run_analysis calls racing on the same key cannot both return True."""
```

Implemented in `SQLReportBackend` as a single conditional statement per
dialect (matching the existing `sqlite_insert`/`pg_insert` split already used
by `upsert_rca_report`):

- Try an `UPDATE ... WHERE dedupe_key = :key AND last_handoff_at <= :cutoff`
  (`:cutoff = now - cooldown_seconds`), setting `last_handoff_at = :now`.
- If that affects 0 rows, try an `INSERT` guarded by `ON CONFLICT DO NOTHING`
  (the row may not exist yet, or may exist with a fresh timestamp that lost
  the race).
- Return `True` only if one of the two statements actually wrote a row.

`cooldown_seconds = 0` always returns `True` (feature effectively disabled
for that call) without touching the table, so `HANDOFF_COOLDOWN_SECONDS=0`
is a clean opt-out.

**The slot is stamped whether the subsequent handoff call succeeds, dedupes,
or throws.** A failing receiver (AE down, network partition) is exactly the
case where repeated retries would be most likely to compound a problem
(`HandoffResult.failed(e)` already records this outcome on the report
today) — treating a failed attempt as still "spent" against the cooldown is
the conservative choice: it protects AE/GitHub during an outage instead of
hammering it with retries every time a new firing comes in.

---

## 5. Suppressed run's report

`report_data["handoff"]` is present with value `None` for a suppressed run
(`RCAReport.handoff` defaults to `None`, and `model_dump()` always emits the
key) — identical in shape to today's `handoff_enabled=False` case, or a
`RootCauseIdentified` check that fails. No new status value, no new field on
`HandoffResult`, no console/UI change. The suppression is visible only via
the `logger.info` line and, indirectly, the `handoff_cooldowns` table — this
was a deliberate scope decision: RCA and remediation still run and publish
their own report in full for every occurrence (see §1's non-goals), so the
existing report already carries the diagnostic history; a suppressed
handoff only means "we chose not to also call out to AE this time."

---

## 6. Alternative considered and rejected: gate at intake, before RCA runs

A pre-RCA gate (checked in `src/api/agent_routes.py`'s `/analyze` handler and
`mcp_server.py`'s `analyze_runtime_state`, before `run_analysis` is ever
scheduled) was considered, since it would also save the RCA/remediation
compute cost on a truly flapping alert, not just the handoff call.

Rejected because it requires a coarse key built only from intake-time data —
something like `component + alert rule name`, since the precise fingerprint
doesn't exist until RCA has actually queried logs and found the error
signature. `AE-HANDOFF-DESIGN.md` §9 documents this exact failure mode
already happening once in this project's history: the dedupe key used to be
component-only, and "any two incidents on the same service folded onto one
open issue regardless of root cause — a genuinely different bug was silently
suppressed until the first issue was closed." §14 of that document lists
several other cases where a decision was deliberately moved *later* in the
pipeline after an early, coarse version of it caused a live incident. A
pre-RCA gate reintroduces that same shape of risk: two distinct root causes
tripping the same alert rule on the same component would collide on the
coarse key, and the second would be dropped before anyone — human or
agent — ever looked at it.

If RCA-stage compute cost from flapping alerts becomes a real, measured
problem later, the safer fix is a narrow, separate idempotency guard on
literal duplicate delivery (the same `alert.id` arriving twice within
seconds, e.g. an at-least-once webhook retry) — not a business-logic "is
this the same incident" dedup at intake. That is out of scope for this
design; nothing here precludes adding it later as its own change.

---

## 7. Config

`src/config.py`, alongside the existing `handoff_*` settings:

```python
handoff_cooldown_seconds: int = 1800   # 30 min; 0 disables the gate
```

No new startup validation needed — unlike `handoff_enabled`'s dependents
(`handoff_api_url`, `handoff_provider_file`, `external_skills_dir`), a
missing or zero cooldown degrades to today's always-call behavior rather
than failing to start.

---

## 8. Testing

- `try_acquire_handoff_slot`: first call for a fresh key returns `True`;
  immediate second call returns `False`; call after `cooldown_seconds` has
  elapsed returns `True` again; two concurrent calls on the same fresh key
  resolve to exactly one `True`.
- `cooldown_seconds=0` always returns `True`.
- `run_analysis`: a second run within the cooldown window for the same
  `(project, component, fingerprint)` does not invoke `HANDOFF_AGENT` and
  leaves `report_data["handoff"]` as `None`; RCA and remediation still ran and
  were recorded.
- A different fingerprint, component, or project within the same window is
  not suppressed.
- A failed handoff call (exception path) still stamps the cooldown slot.
- The manual `analyze_runtime_state` path is gated identically to the
  alert-driven path when both land on the same dedupe key.
