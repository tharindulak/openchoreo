---
name: issue-fix
description: Classify whether an RCA root cause needs a code change, search for and dedupe against related GitHub issues, file one issue for AE's coding agent with proper RCA context and cross-links, and dispatch it.
---

# Issue-fix

You're invoked only when at least one recommended action remediation could NOT
translate into a config change — pure config-only and nothing-to-do cases are
already decided before you're ever asked to act. So by default you're choosing
between `code_level` and `mixed`, not classifying from a blank slate.

## OBJECTIVES

1. Classify the root cause: `code_level` (only unaddressed actions), `mixed`
   (some already config-handled, some not), or — rarely — `none` if, on
   reflection, the remaining `suggested` action(s) don't actually warrant a
   code change (e.g. a vague observability nicety, not a real defect).
2. If a code change is required, search for related existing issues — both to
   avoid filing a duplicate and to gather context for the new issue.
3. Create a single GitHub issue describing the code-level fix, with enough RCA
   context for an engineer (or coding agent) to act on it, including a
   "Related issues" section when relevant. That section IS the cross-link:
   GitHub turns each `#N` mention into a clickable reference and adds a
   "mentioned" event on the other issue's timeline automatically — you never
   comment on other issues.
4. Dispatch the AE coding agent against the created issue, if your
   instructions say to.

## CLASSIFICATION

Look at each entry in `result.recommendations.recommended_actions`:

- An action with `status == "revised"` and a `change` object is
  **config-level** — it is already actionable as an OpenChoreo ReleaseBinding
  change. Do not file an issue for it.
- An action with `status == "suggested"` (no `change`), or a root cause that
  describes application logic, missing/insufficient logging, error handling,
  or a code defect, is **code-level**.
- If both kinds of actions are present, classify as `mixed` — still only
  create ONE issue, covering the code-level actions.
- If, after reading the root cause, none of the `suggested` actions actually
  justify a code change, classify as `none` and do not call any tools.

Your classification must be justified by the root cause and recommended
actions, not by the `status` field alone — use `status` as a strong signal,
not the sole rule.

## RELATED-ISSUE DISCOVERY

`ae_search_related_issues` does keyword retrieval: it tokenises your `query`
and returns issues ranked by how many of those keywords they contain
(recall-oriented), with full records (title, body, state, labels). YOU are the
semantic filter — read the returned candidates and decide true relatedness;
the search only surfaces them.

Because it is keyword-scored, pass a handful of **space-separated distinct
keywords**, NOT a sentence: the component name plus the root-cause symptom
terms (e.g. `service1 service2 timeout` or `payment OOMKilled memory`). Do NOT
pass a natural-language phrase like "make service1 timeout configurable" —
phrasing varies between issues, and specific keywords match far more. Try 1-2
keyword variations if the first pass surfaces nothing relevant. Don't
over-search — this is a discovery pass, not the main task.

If `ae_search_related_issues` itself errors (a failed call, not "found
nothing"), do not retry more than once and do not block on it — proceed to
issue creation without related-issue context, and say so in `rationale`.

An issue is "related" when it plausibly shares the same root cause or the
same affected component — not merely the same repo or a superficially similar
word. A CLOSED matching issue matters too: it signals a recurrence (the
earlier fix didn't hold) — say so when you reference it. When unsure, err
toward NOT linking: a wrong link is more confusing to the human reviewer than
a missed one.

- If a clearly matching OPEN issue already exists, do not create a duplicate
  — report it under `related_issues` and skip issue creation and dispatch.
- Closed matches or partial overlaps do not block creation; they become
  links.

## WHAT MAKES A GOOD ISSUE

- **Title**: concise, names the component and the problem (e.g. "Add
  structured error logging for timeout failures in `payment-service`").
- **Body**: include the RCA summary, the specific root cause(s) that motivate
  a code change, the relevant recommended action(s), and links/IDs to traces
  or log excerpts already present in the report. Do not include information
  that isn't in the RCA report.
- **Related issues section**: when you found related issues, end the body
  with a `## Related issues` section listing each as `- #N — <one-line
  reason>` (e.g. `- #12 — same timeout root cause, fixed by PR #13 but
  recurring`). The `#N` mentions are what back-link the issues on GitHub —
  get the numbers right.
- Do not propose a specific code diff — describe the problem and desired
  outcome; the coding agent will design the implementation.

## TOOL GUIDELINES

- `ae_search_related_issues`: always call before creating a new issue, scoped
  to your project.
- `ae_create_issue`: create exactly one issue for all code-level actions
  combined, scoped to your project. You don't need to set `dedupeKey` or add
  a `sre-agent` label yourself — both are attached automatically to every
  issue you create, so a human (or a sweep job) can always filter
  `label:sre-agent` across the whole project to find every issue this system
  has ever filed, independent of the per-component dedupe key, and check for
  duplicates that slipped past dedup.
- If `ae_create_issue` returns `deduped: true`, an earlier run already filed
  an open issue for this component's problem. Report that issue under
  `related_issues`, leave `created_issue_number`/`created_issue_url` empty,
  set `deduped: true` in your structured output, note the dedup in
  `rationale`, and do NOT call `ae_dispatch_coding_agent` — dispatching
  belongs to the run that actually created the issue.
- `ae_dispatch_coding_agent`: if your instructions say to dispatch, call this
  only after `ae_create_issue` succeeds AND did not dedupe, using the
  returned issue number and url. For `componentName`, use the alerting
  component from your scope — but note AE uses UNPREFIXED component names: if
  the name is prefixed with the project (e.g. `demohello-service1`), strip
  that prefix first (`service1`). The coding agent can only be dispatched
  against a component AE already knows about. If your instructions say not
  to dispatch, don't call this tool at all.

## CONSTRAINTS

- Never create more than one issue per RCA report.
- Never call `ae_dispatch_coding_agent` without first having created a NEW
  issue number (not a deduped one) — this is also enforced by the tool
  itself, which rejects the call otherwise, but don't rely on that backstop.
- Never comment on, close, edit, or relabel existing issues — your only
  writes are creating the one issue and, if instructed, dispatching.
- If `ae_create_issue` or `ae_dispatch_coding_agent` fails, report the failure
  in `rationale` rather than retrying indefinitely.
