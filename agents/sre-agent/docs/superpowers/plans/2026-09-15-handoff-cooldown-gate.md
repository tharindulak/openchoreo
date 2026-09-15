# Handoff Cooldown Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `run_analysis` from calling the handoff MCP server (and, through it, AE's GitHub API calls) more than once per `cooldown_seconds` window for the same underlying incident, without changing anything about RCA or remediation.

**Architecture:** A new `try_acquire_handoff_slot(dedupe_key, cooldown_seconds)` method on `ReportBackend`, backed by a new `handoff_cooldowns` table in the existing SQL backend, does an atomic check-and-stamp. `run_analysis` calls it right before invoking `HANDOFF_AGENT`, keyed on `f"{project}/{component}/{fingerprint}"`; a `False` result skips the handoff call entirely and leaves `report_data["handoff"]` absent, exactly as if `handoff_enabled` were `False`.

**Tech Stack:** Python 3.14, SQLAlchemy async (sqlite+aiosqlite / postgresql+asyncpg), pytest + pytest-asyncio.

**Spec:** `agents/sre-agent/HANDOFF-COOLDOWN-DESIGN.md`

## Global Constraints

- No change to RCA or remediation behavior — they run in full on every request, cooldown or not.
- No change to AE's own dedupe/reopen/classification logic — this is a caller-side throttle only.
- `handoff_cooldown_seconds=0` must always allow the handoff call through (opt-out), without touching the database.
- The slot must be stamped whether the subsequent handoff call succeeds, dedupes, or throws.
- A suppressed run leaves `report_data["handoff"]` absent — no new status value, no console-visible change.
- Dedupe key format is exactly `f"{scope.project}/{scope.component}/{fingerprint or 'nofp'}"`.

---

### Task 1: Add `handoff_cooldown_seconds` config setting

**Files:**
- Modify: `src/config.py:88` (immediately after `handoff_enabled: bool = False`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.handoff_cooldown_seconds: int`, default `1800`, consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_handoff_cooldown_seconds_defaults_to_thirty_minutes():
    assert Settings().handoff_cooldown_seconds == 1800


def test_handoff_cooldown_seconds_is_configurable():
    assert Settings(handoff_cooldown_seconds=0).handoff_cooldown_seconds == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k handoff_cooldown_seconds -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'handoff_cooldown_seconds'` (or a pydantic "extra fields not permitted" style error, since `model_config` sets `extra="allow"` — either way, not the value `1800`).

- [ ] **Step 3: Write minimal implementation**

In `src/config.py`, replace:

```python
    remed_agent: bool = False
    handoff_enabled: bool = False
```

with:

```python
    remed_agent: bool = False
    handoff_enabled: bool = False
    # Minimum time between handoff (search+create) calls for the same
    # project/component/fingerprint dedupe key — see
    # HANDOFF-COOLDOWN-DESIGN.md. A flapping alert still runs full RCA every
    # time; this only throttles the repeated round-trip to the handoff MCP
    # server (and, through it, AE's GitHub API calls) for the same incident.
    # 0 disables the gate: every call goes through, matching pre-cooldown
    # behavior.
    handoff_cooldown_seconds: int = 1800
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -k handoff_cooldown_seconds -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: add handoff_cooldown_seconds config setting"
```

---

### Task 2: Add `try_acquire_handoff_slot` to the `ReportBackend` interface

**Files:**
- Modify: `src/clients/backend/report_backend.py`

**Interfaces:**
- Produces: abstract method `ReportBackend.try_acquire_handoff_slot(dedupe_key: str, cooldown_seconds: int) -> bool`, implemented by Task 3, called by Task 4.

This task has no independent test of its own — an ABC method has no behavior to test until it's implemented. It exists as its own task so the interface change is reviewable separately from the SQL implementation.

- [ ] **Step 1: Add the abstract method**

In `src/clients/backend/report_backend.py`, add after `list_rca_reports`:

```python
    @abstractmethod
    async def try_acquire_handoff_slot(
        self,
        dedupe_key: str,
        cooldown_seconds: int,
    ) -> bool:
        """Atomically check-and-stamp a handoff attempt for ``dedupe_key``.

        Returns True (caller may proceed with the handoff call) only if no
        prior stamp exists for this key, or the prior stamp is older than
        ``cooldown_seconds``. Stamps "now" in the same atomic operation, so
        two concurrent callers racing on the same key cannot both receive
        True. ``cooldown_seconds <= 0`` always returns True without writing
        anything.
        """
        ...
```

- [ ] **Step 2: Run the full test suite to confirm nothing else implements this ABC yet**

Run: `uv run pytest tests/ -v`
Expected: FAIL — `SQLReportBackend` can no longer be instantiated (`TypeError: Can't instantiate abstract class SQLReportBackend without an implementation for abstract method 'try_acquire_handoff_slot'`) wherever a test constructs it (e.g. `tests/test_sql_backend.py`'s `backend` fixture). This confirms the abstract method is correctly wired before Task 3 implements it.

- [ ] **Step 3: Commit**

```bash
git add src/clients/backend/report_backend.py
git commit -m "feat: declare try_acquire_handoff_slot on the ReportBackend interface"
```

---

### Task 3: Implement `try_acquire_handoff_slot` in `SQLReportBackend`

**Files:**
- Modify: `src/clients/backend/sql_backend.py`
- Test: `tests/test_sql_backend.py`

**Interfaces:**
- Consumes: `ReportBackend.try_acquire_handoff_slot` signature from Task 2.
- Produces: a working `handoff_cooldowns` table and method, consumed by Task 4's `run_analysis` wiring.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sql_backend.py`:

```python
@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_first_call_succeeds(backend):
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is True


@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_second_call_within_cooldown_fails(backend):
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is True
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is False


@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_succeeds_again_after_cooldown_elapses(backend):
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is True
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is False
    # A cooldown of 0 seconds elapses immediately, so the next call is
    # already past its own window.
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 0) is True


@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_zero_cooldown_always_succeeds(backend):
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 0) is True
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 0) is True


@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_different_keys_are_independent(backend):
    assert await backend.try_acquire_handoff_slot("p/c/fp1", 1800) is True
    assert await backend.try_acquire_handoff_slot("p/c/fp2", 1800) is True
    assert await backend.try_acquire_handoff_slot("p/other/fp1", 1800) is True


@pytest.mark.asyncio
async def test_try_acquire_handoff_slot_concurrent_calls_on_a_fresh_key_only_one_succeeds(
    backend,
):
    results = await asyncio.gather(
        backend.try_acquire_handoff_slot("p/c/fp1", 1800),
        backend.try_acquire_handoff_slot("p/c/fp1", 1800),
    )
    assert sorted(results) == [False, True]
```

Add `import asyncio` to the top of `tests/test_sql_backend.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sql_backend.py -k try_acquire_handoff_slot -v`
Expected: FAIL — `AttributeError: 'SQLReportBackend' object has no attribute 'try_acquire_handoff_slot'`.

- [ ] **Step 3: Write the implementation**

In `src/clients/backend/sql_backend.py`, add the new table after the `rca_reports` table definition:

```python
handoff_cooldowns = Table(
    "handoff_cooldowns",
    metadata,
    Column("dedupe_key", String, primary_key=True),
    Column("last_handoff_at", String, nullable=False),  # ISO-8601, UTC
)
```

Add `from datetime import timedelta` to the existing `from datetime import UTC, datetime` import line, making it:

```python
from datetime import UTC, datetime, timedelta
```

Add the method to `SQLReportBackend`, after `list_rca_reports` and before `close`:

```python
    async def try_acquire_handoff_slot(
        self,
        dedupe_key: str,
        cooldown_seconds: int,
    ) -> bool:
        if cooldown_seconds <= 0:
            return True

        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=cooldown_seconds)).isoformat()
        now_str = now.isoformat()

        async with self.engine.begin() as conn:
            update_stmt = (
                handoff_cooldowns.update()
                .where(
                    handoff_cooldowns.c.dedupe_key == dedupe_key,
                    handoff_cooldowns.c.last_handoff_at <= cutoff,
                )
                .values(last_handoff_at=now_str)
            )
            update_result = await conn.execute(update_stmt)
            if update_result.rowcount:
                return True

            if self._is_sqlite:
                insert_stmt = sqlite_insert(handoff_cooldowns).values(
                    dedupe_key=dedupe_key, last_handoff_at=now_str
                )
            else:
                insert_stmt = pg_insert(handoff_cooldowns).values(
                    dedupe_key=dedupe_key, last_handoff_at=now_str
                )
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=["dedupe_key"]
            )
            insert_result = await conn.execute(insert_stmt)
            return bool(insert_result.rowcount)
```

The `UPDATE` covers "an existing, expired stamp" (rowcount 1 → proceed) and "an existing, fresh stamp" (rowcount 0, correctly falls through to a no-op insert). The `INSERT ... ON CONFLICT DO NOTHING` covers "no stamp yet" — exactly one of two concurrent inserts on the same fresh key wins the primary-key constraint, so the loser's `insert_result.rowcount` is genuinely `0` rather than raising, giving the race-safety the interface promises.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sql_backend.py -k try_acquire_handoff_slot -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `uv run pytest tests/test_sql_backend.py -v`
Expected: PASS (all tests, including the pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add src/clients/backend/sql_backend.py tests/test_sql_backend.py
git commit -m "feat: implement try_acquire_handoff_slot in SQLReportBackend"
```

---

### Task 4: Wire the cooldown gate into `run_analysis`

**Files:**
- Modify: `src/agent/agent.py:480-551`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `settings.handoff_cooldown_seconds` (Task 1), `report_backend.try_acquire_handoff_slot` (Tasks 2-3), `error_fingerprint` (existing, `src/agent/fingerprint.py` — unchanged).

- [ ] **Step 1: Write the failing tests**

In `tests/test_agent.py`, first fix the existing handoff test so it keeps passing once the gate is wired in — a `MagicMock()`'s auto-generated attributes are not awaitable, so `await backend.try_acquire_handoff_slot(...)` needs an explicit `AsyncMock`. Change `test_run_analysis_records_a_failed_handoff_on_the_report`:

```python
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
```

(Only the added `backend.try_acquire_handoff_slot = AsyncMock(return_value=True)` line changes.)

Then add three new tests after it:

```python
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
    saved = backend.upsert_rca_report.await_args.kwargs["report"]
    assert "handoff" not in saved


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
    assert "handoff" in saved


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
```

`SCOPE` (already defined at the top of `tests/test_agent.py`) has `project="p"` and `component="c"`, matching the literal keys asserted above.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k "cooldown or dedupe_key" -v`
Expected: FAIL — `try_acquire_handoff_slot` is never called yet (`AssertionError: Expected 'try_acquire_handoff_slot' to have been called once. Called 0 times.` / the `handoff_create.assert_not_called()` test currently fails because `HANDOFF_AGENT.create` IS called today with no gate in front of it).

- [ ] **Step 3: Write the implementation**

In `src/agent/agent.py`, replace the entire block from `if settings.handoff_enabled and isinstance(rca_report.result, RootCauseIdentified):` through the matching `except Exception as e:` / `logger.error(...)` (today lines 480-551):

```python
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
                fingerprint = error_fingerprint(report_data, raw_log_lines)
                dedupe_key = f"{scope.project}/{scope.component}/{fingerprint or 'nofp'}"

                if not await report_backend.try_acquire_handoff_slot(
                    dedupe_key, settings.handoff_cooldown_seconds
                ):
                    logger.info(
                        "Handoff suppressed: cooldown active for dedupe key %s",
                        dedupe_key,
                    )
                else:
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
                                        "signature": fingerprint,
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
                        if handoff_logging and (
                            summary_line := handoff_logging.tool_call_summary()
                        ):
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
                        logger.error(
                            "Handoff agent failed, recorded on the RCA report: %s", e
                        )
```

This is a pure reindent-plus-gate of the existing block: everything from `try:` onward moves one level deeper under the new `else:`, `error_fingerprint(...)` is computed once into `fingerprint` instead of inline inside `build_handoff_headers`'s dict literal, and the new `dedupe_key` / `try_acquire_handoff_slot` check sits between the (unchanged) `action_statuses` computation and the (unchanged) handoff call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones and the new/modified ones from Step 1)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/agent/agent.py tests/test_agent.py
git commit -m "feat: gate the handoff call behind a per-incident cooldown"
```
