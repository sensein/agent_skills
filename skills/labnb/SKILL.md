---
name: labnb
description: Create and maintain a concurrency-safe global lab notebook outside project roots, with idea capture, startup summaries of prior work, append-only indexing, and isolated experiment workspaces across projects, investigations, and tasks.
---

# Lab Notebook

Use this skill when the user wants a reusable lab notebook that survives across tasks, projects, investigations, and sessions.

The key difference from repo-local `.lab/` workflows is that the lab root lives in a general location and must stay safe under parallel use.

## Constitution

This skill must inherit and obey any higher-level constitution, parent skill, project policy, task instruction, or user constraint already in scope.

Before taking action:

1. Review the parent instructions that govern the current task.
2. Review the notebook index for related ideas and experiments.
3. If the parent rules are stricter than this skill, follow the stricter rule.
4. If this skill is missing a guardrail required by the parent context, add the missing guardrail locally in your plan instead of assuming it is safe.

This skill never overrides:

- user instructions
- project-specific safety or contribution rules
- higher-level constitutions about writes, approvals, secrets, or external side effects

## Subskills

Use these focused subskills when a narrower task is enough:

- [`labnb-resume`](./subskills/labnb-resume/SKILL.md): summarize prior ideas and experiments for a project and decide where to pick up
- [`labnb-idea`](./subskills/labnb-idea/SKILL.md): register an unimplemented experiment idea in the shared notebook
- [`labnb-promote`](./subskills/labnb-promote/SKILL.md): turn an existing idea into a concrete experiment with explicit budgets and provenance
- [`labnb-run`](./subskills/labnb-run/SKILL.md): create and run a concrete experiment with budgets, isolated workspace, and iteration logging

## Default Root

Use this location unless the user explicitly wants another path:

```bash
${LAB_NOTEBOOK_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/lab-notebook}
```

This keeps the notebook outside any single project tree and outside any specific agent runtime, while still making it easy to override.

## Interactive Setup Gate

Before starting a new experiment, gather or infer these fields:

1. Goal
2. Mechanical metric
3. Direction: higher or lower is better
4. Verify command
5. Writable scope
6. Workspace placement: use the default notebook workspace path, or link the workspace to a different location for large clones, datasets, or rich outputs
7. Time budget or timebox, if the user gave one
8. Stop condition: target metric, iteration count, checkpoint, or manual stop

If any of the first four are missing and cannot be inferred safely, ask before starting. The notebook is global, but the experiment loop still needs a clear local objective.

If the user gives a time budget, treat it as a hard ceiling, not a target to consume.

The setup must answer three planning questions early:

1. What is the smallest useful iteration that can produce information or a decision?
2. What checkpoint would justify continuing into a second iteration?
3. If the budget is too small for even one useful iteration, say so clearly instead of pretending the work fits.

Treat the budget as applying to the whole proposed path, including any parallel branches, delegated work, or downstream follow-up experiments that you are suggesting now.

## Required Guarantees

1. Never use a fixed experiment directory name.
2. Never let two runs compete to rewrite the central index without a lock.
3. Never store the only copy of experiment state inside the project being studied.
4. Never delete or rewrite prior experiment rows from the central index.
5. Prefer deterministic helpers in [`scripts/register_experiment.py`](./scripts/register_experiment.py) and [`scripts/summarize_index.py`](./scripts/summarize_index.py) instead of ad hoc shell snippets.
6. Track labnb-managed creates and updates with best-effort provenance using W3C PROV-O terms inside each entry directory.
7. Require explicit user confirmation before labnb performs deletions.
8. Treat provenance as the source of truth for monitored slice state; do not rely on a separate mutable loop-state file.

## Local Guardrails

Apply these guardrails even when the parent constitution does not spell them out explicitly:

1. Review the notebook index and the active task constitution before starting new work.
2. Never edit the original source tree directly when the experiment is supposed to use an isolated workspace.
3. If project files may change, clone or copy the source into the experiment workspace first and do the edits there.
4. Never let two active experiments write to the same clone, checkout, branch working tree, or results directory.
5. Treat parallel operations as potentially conflicting until the write scopes are proven disjoint.
6. If multiple experiments touch the same upstream source, keep their workspaces separate and link them only through notebook metadata, not shared writes.
7. Preserve append-only notebook history; record new state with new rows or child entries instead of rewriting prior conclusions.
8. Before resuming or branching, summarize related entries so you understand what has already been tried and what constraints still apply.
9. If the safe workspace strategy is unclear, pause and choose the safer option rather than writing into an ambiguous location.
10. Keep secrets, auth state, and external credentials outside experiment artifacts unless the parent constitution explicitly allows otherwise.
11. Treat provenance as best-effort only: external changes or deletions may still occur without labnb observing them.
12. Do not perform notebook-managed deletions without explicit confirmation from the user.

## Lab Layout

```text
lab-notebook/
  ideas/
    <idea-id>/
      metadata.json
      idea.md
      provenance.jsonl
      provenance.md
  experiments/
    <experiment-id>/
      metadata.json
      plan.md
      log.md
      provenance.jsonl
      provenance.md
      results.tsv
      summary.md
      artifacts/
  workspaces/
    <experiment-id>/
  index/
    experiments.tsv
    index.md
  locks/
```

- `ideas/<idea-id>/`: not-yet-started or not-yet-promoted experiment ideas
- `experiments/<experiment-id>/`: one experiment per directory; no sharing across active runs
- `provenance.jsonl`: best-effort append-only provenance log using W3C PROV-O terms for labnb-managed actions
- `provenance.md`: human-readable provenance policy and caveats
- `experiments/<experiment-id>/plan.md`: the setup gate plus current hypothesis
- `experiments/<experiment-id>/results.tsv`: local iteration ledger for this experiment
- `workspaces/<experiment-id>/`: per-experiment working area for an isolated project clone or checkout
- `index/experiments.tsv`: append-only registry of ideas and experiments
- `index/index.md`: generated readable summary of known entries
- `locks/`: lock directories used for central updates

## Experiment Identity

Each experiment id must be unique and stable:

```text
<utc-timestamp>--<project-slug>--<experiment-slug>--<random-suffix>
```

Example:

```text
20260411T154512Z--speech-foundations--prosody-ablation--a1b2c3d4
```

The random suffix is mandatory. A timestamp plus slug alone is not enough for parallel work.

## Setup Flow

1. Resolve the global lab root.
2. Review the parent constitution, project rules, and any task-specific guardrails that apply to this run.
3. Derive a `project_slug` from the current project, investigation, source tree, or working directory name.
4. Pick an `experiment_slug` that describes the current investigation.
5. Before creating a new experiment, summarize the existing notebook entries for the project:

```bash
python skills/labnb/scripts/summarize_index.py \
  --lab-root "$LAB_ROOT" \
  --project-slug "$PROJECT_SLUG"
```

6. Use that summary to decide whether to:
   - resume an existing experiment
   - promote an existing idea into an experiment
   - create a child experiment from a prior run
   - or start a new experiment
7. Create the entry by running the helper:

```bash
python skills/labnb/scripts/register_experiment.py \
  --lab-root "$LAB_ROOT" \
  --project-root "$PWD" \
  --project-slug "$PROJECT_SLUG" \
  --experiment-slug "$EXPERIMENT_SLUG" \
  --objective "Short statement of the experiment goal" \
  --entry-kind experiment \
  --metric-name "$METRIC_NAME" \
  --direction "$DIRECTION" \
  --verify-command "$VERIFY_COMMAND" \
  --overall-budget "$OVERALL_BUDGET" \
  --loop-budget "$LOOP_BUDGET" \
  --source-id "$SOURCE_ENTRY_ID"
```

8. To record an idea that is not yet being run, use `--entry-kind idea` and omit experiment-only fields that are still unknown.
9. To promote an existing idea into a concrete experiment, use the dedicated helper:

```bash
python skills/labnb/scripts/promote_idea.py \
  --lab-root "$LAB_ROOT" \
  --idea-id "$IDEA_ID" \
  --project-root "$PROJECT_ROOT" \
  --experiment-slug "$EXPERIMENT_SLUG" \
  --metric-name "$METRIC_NAME" \
  --direction "$DIRECTION" \
  --verify-command "$VERIFY_COMMAND" \
  --overall-budget "$OVERALL_BUDGET" \
  --loop-budget "$LOOP_BUDGET"
```

10. Experiments must be created with explicit `--overall-budget` and `--loop-budget`; do not leave them unspecified.
11. Use one or more `--source-id` flags when an experiment stems from prior ideas or experiments.
12. Start the monitored loop slice before the first write-bearing iteration:

```bash
python skills/labnb/scripts/monitor_slice.py start \
  --experiment-dir "$EXPERIMENT_DIR"
```

13. Use `monitor_slice.py check` before another iteration or long-running verify step, and `monitor_slice.py finish` when the slice ends.
14. If the user wants the editable clone or large outputs elsewhere, pass `--workspace-root "$WORKSPACE_ROOT"` and let the notebook create a stable link under `workspaces/<experiment-id>/`.
15. If code changes are involved, clone or copy the relevant source tree into the experiment's dedicated workspace path before editing.
16. If multiple agents are working on the same codebase, each agent must use its own separate clone location. Never share a single git checkout across active agents.
17. Record baseline iteration `0` in `results.tsv` before code changes.
18. Work inside the returned experiment directory for notes, artifacts, and summaries.
19. Log progress inside that experiment directory, not in shared files.

## Configurable Options

The skill has two kinds of configurable inputs: notebook root settings and per-experiment registration options.

### Root Settings

- `LAB_NOTEBOOK_ROOT`: override the default global notebook location
- `XDG_STATE_HOME`: fallback base for the default notebook root when `LAB_NOTEBOOK_ROOT` is unset

### Registration Options

Use these helper flags when creating an experiment:

- `--lab-root`: explicit notebook root
- `--project-root`: source tree or task directory being studied; optional for ideas that are still abstract or for work that will create a new repo later
- `--project-slug`: stable short name for the project
- `--experiment-slug`: short name for this idea or experiment entry
- `--objective`: concise goal statement
- `--entry-kind`: `experiment` or `idea`
- `--status`: optional explicit state; defaults to `started` for experiments and `ideation` for ideas
- `--metric-name`: optional metric label
- `--direction`: optional optimization direction such as `higher` or `lower`
- `--verify-command`: optional mechanical check command
- `--overall-budget`: required for experiments; total budget for the whole experiment path, including proposed follow-up or parallel work unless deferred
- `--loop-budget`: required for experiments; budget or timebox for the current loop or iteration slice
- `--workspace-root`: optional external root for the real workspace location
- `--source-id`: repeatable link to one or more upstream ideas or experiments that this run stems from
- `--parent-id`: optional parent experiment id for child experiments

If `--metric-name`, `--direction`, or `--verify-command` are omitted, the helper records `TBD` in `plan.md`. For experiments, `--overall-budget` and `--loop-budget` must be specified at creation time.

Recommended statuses include:

- `ideation`
- `planned`
- `started`
- `stopped`
- `completed`
- `terminated`
- `crashed`
- `deferred`
- `promoted`
- `archived`

The summary helper also supports:

- `--lab-root`: explicit notebook root
- `--project-slug`: project to summarize
- `--limit`: maximum number of recent matching entries to show

The slice monitor helper supports:

- `start`: begin a monitored loop slice
- `check`: refresh elapsed state and stop when the slice or overall budget is exhausted
- `finish`: close the active slice with a final status

## What Goes In Each Entry

For ideas:

- `metadata.json`: creation metadata, status, budgets, provenance mode, and linkage to future work
- `idea.md`: rationale, prior evidence to revisit, and pickup criteria for promotion into an experiment
- `provenance.jsonl`: append-only best-effort PROV-O event log
- `provenance.md`: provenance rules and deletion policy

For experiments:

- `metadata.json`: creation metadata, source path context, objective, ids, status, provenance mode, and `source_ids`
- `plan.md`: goal, metric, direction, verify command, scope, and next hypothesis
- `log.md`: chronological notes for the experiment
- `provenance.jsonl`: append-only best-effort PROV-O event log
- `provenance.md`: provenance rules and deletion policy
- `results.tsv`: one row per iteration or thought with status and metric outcome
- `summary.md`: final concise outcome
- `artifacts/`: scratch outputs, plots, reports, and temporary files worth keeping
- dedicated workspace clone path: a safe place to edit without colliding with another agent's git state
- optional workspace link path under the notebook: a stable pointer when the real workspace lives elsewhere

Keep detailed notes local to the idea or experiment directory. The global index should stay compact.

## Improvement Loop

Use the same tight loop pattern that powers autoresearch, but anchor it in the global notebook:

1. Observe: run the index summary first if you have not already done so for this project, then read `plan.md`, the tail of `log.md`, `results.tsv`, and relevant project state.
2. Re-check the parent constitution and project rules before any write-bearing step.
3. Start or check the monitored slice with `monitor_slice.py`; use provenance as the source of truth for remaining time.
4. Confirm you are working inside the experiment's dedicated clone if project files will change.
5. Convert any user time budget into a bounded iteration plan:
   - pick the smallest useful first slice
   - define the checkpoint for continuing
   - leave explicit slack for logging, verification, and handoff
   - if no useful slice fits, record that the budget is insufficient
6. Pick one focused change. Prefer atomic edits so the outcome is explainable.
7. If tracked project files change, commit before verification so rollback is cheap.
8. Run the verify command and capture the metric.
9. Keep or discard:
   - keep when the metric improves
   - keep when the metric ties and the result is clearly simpler
   - discard or revert when the metric regresses or the run crashes
10. Log the outcome in both `results.tsv` and `log.md`.
11. Run `monitor_slice.py check` before continuing, and `monitor_slice.py finish` when the slice ends.
12. Decide whether another iteration is justified, rather than expanding work to fill the remaining budget.
13. Repeat until the stop condition is reached, the next checkpoint fails, the budget is exhausted, or the user interrupts.

When the work diverges materially, register a child experiment instead of overloading the current one.

## Budget And Iteration Policy

Use time budgets to shape the experiment, not to maximize activity.

1. A budget is a cap on work, not a requirement to use all allotted time.
2. Prefer a plan with one informative first slice and one optional next slice over a long speculative roadmap.
3. Reserve some of the budget for setup, verification, logging, and summarizing.
4. Include any proposed parallel experiments, child experiments, or downstream follow-up in the same budget reality check unless you state clearly that they are outside the current budget.
5. When an experiment draws on multiple ideas or prior runs, list all of them as source entries at creation time.
6. If the likely useful first slice already exceeds the available budget, say the plan is not feasible as stated.
7. If the task is open-ended, propose a shortest credible iteration path and stop at the first decision point.
8. When reporting the plan, distinguish:
   - what fits now
   - what becomes possible only if the first slice succeeds
   - what is out of scope for the current budget

## Shared Index Rules

The central index exists to answer: what ideas and experiments exist, where are they, and what are they about?

When creating or updating the shared index:

1. Acquire `locks/index.lock` using an atomic directory create.
2. Append exactly one row per new idea or experiment to `index/experiments.tsv`.
3. Rebuild `index/index.md` from `experiments.tsv` while the lock is held.
4. Write the regenerated markdown to a temp file and rename it into place atomically.
5. Release the lock even on failure.
6. Keep status current in new rows or explicit follow-up events rather than silently mutating history.

If a lock appears stale, only clear it after confirming the owning process is gone. Prefer waiting over forcing.

## Parallel Work Rules

1. Every active experiment gets its own directory.
2. Two agents may read the same project, but they must not share the same new experiment directory.
3. If two agents will both edit the same codebase, each one must create a separate clone or checkout under its own `workspaces/<experiment-id>/` path.
4. If multiple agents need related work, give each one a new experiment id and link them in notes instead of co-writing.
5. Treat `index/experiments.tsv` as append-only history.
6. If an experiment needs a follow-up, create a child experiment and record the parent id in `metadata.json` or `log.md`.

## Resume Flow

When resuming:

1. Run the summary helper or read `index/index.md` for the project slug.
2. Decide whether the best pickup point is a planned idea, an active experiment, or a completed experiment worth branching from.
3. Open the target idea or experiment directory.
4. Continue writing only inside that directory unless you are registering a new idea or experiment.
5. Register a fresh experiment instead of mutating old metadata if the scope or hypothesis changed materially.

## Notes For Agents

- Prefer the helper script over handwritten lock logic when the script is available.
- If you need a quick view of the notebook, run `scripts/summarize_index.py` or read `index/index.md` first and only inspect specific entry directories afterward.
- Before starting a new experiment, summarize relevant prior ideas and experiments and say where you are picking up from.
- When a run stems from multiple ideas or prior experiments, register all of them as `source_id` links instead of collapsing them into a single parent.
- Review the parent constitution and local project guardrails before planning or writing.
- Use the experiment's dedicated workspace path for editable project state; do not point multiple active agents at the same clone.
- Clone or copy source into the experiment workspace before editing whenever the source tree must stay isolated.
- Ask whether the workspace should live somewhere else when a source clone, datasets, or generated artifacts would be better outside the notebook root.
- Re-use the same experiment directory for iterative logging, but create a new child experiment when you need a new loop with a new hypothesis or project state.
- Record unimplemented but promising directions as ideas instead of forcing them into active experiments.
- Track labnb-managed changes in provenance files, but say clearly that external changes may exist outside that record.
- Use W3C PROV-O terms in provenance records rather than ad hoc event fields.
- Use provenance as the source of truth for monitored slice state and remaining budget.
- Require explicit confirmation before deleting entry files, artifacts, or workspaces through labnb-driven actions.
- If the user gives a budget like "two hours," do not stretch the plan to fill two hours by default; start with the smallest decision-making slice and say when the budget is insufficient.
- If you propose parallel branches or downstream experiments, count them against the same stated budget unless you explicitly mark them as later follow-up outside the current scope.
- Keep instructions concise in user-facing updates; the notebook should do the long-term memory work.
