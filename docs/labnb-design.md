# LabNB Skill Design

## Goal

Create a named skill, `labnb`, that adapts the repo-local `.lab/` workflow into a global lab notebook that:

- lives outside project trees
- tracks experiments across unrelated projects, investigations, and tasks
- tracks not-yet-run experiment ideas alongside active or completed experiments
- keeps a simple human-readable experiment index
- avoids conflicting writes when multiple experiments run in parallel
- lives under `skills/` in this repository so it can be mirrored into agent skill directories easily
- is broken into focused subskills with short `labnb-<action>` invocation names for resume, idea capture, idea promotion, and active experiment execution
- mirrors those focused `labnb-<action>` entries as top-level skills under `skills/` so flat Claude Code and Codex installs discover them directly

## Decisions

- [x] Use an agent-neutral global root: `${LAB_NOTEBOOK_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/lab-notebook}`
- [x] Give every experiment a unique immutable directory created atomically
- [x] Keep the global index append-only and update it under a lock
- [x] Regenerate a readable index file with atomic rename while holding the same lock
- [x] Prefer deterministic helper scripts for directory creation and index updates
- [x] Bring over the `observe -> modify -> verify -> keep/discard -> log -> repeat` loop from `uditgoenka/autoresearch`
- [x] Give each experiment its own workspace path so parallel agents never share a git worktree or copied source tree
- [x] Support linking the workspace to an external location while keeping a stable notebook path
- [x] Add a concurrency test that proves parallel registrations do not collide
- [x] Treat user time budgets as ceilings and require a smallest-useful-iteration plan with an explicit infeasibility path
- [x] Apply the same budget realism to proposed parallel branches and downstream child experiments
- [x] Allow an optional loop budget to be recorded directly in experiment registration metadata and plan scaffolding
- [x] Split overall experiment budget from loop budget so the total plan cap is explicit
- [x] Allow the index to hold both ideas and experiments, with a summary step before starting new work
- [x] Require parent-constitution review and local safety guardrails around source isolation, index review, and parallel writes
- [x] Add best-effort provenance scaffolding and explicit status defaults for all notebook entries
- [x] Serialize provenance with W3C PROV-O terms rather than custom event keys
- [x] Require explicit budgets for experiments at creation time
- [x] Allow an experiment to stem from one or more upstream ideas or experiments
- [x] Monitor active slice budgets from provenance rather than a separate mutable state file
- [x] Require a deliberate timer-or-stop decision before leaving background work unattended, and mark exhausted runs explicitly
- [x] Require any new wait job to check for and reuse or replace existing pending waits for the same experiment
- [x] Scaffold entry-local rules and durable memory so each action can re-check them instead of relying on recall
- [x] Prefer minimal asynchronous metric-comparison checkpoints over waiting for full loop completion when comparing alternatives
- [x] Allow parallel subagent follow-up experiments for comparisons, while counting their resource usage against the same budget
- [x] Require enough instrumentation and external checks that long-running work is not left blind by default

## Layout

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

## Conflict-Avoidance Rules

1. Never create experiment directories from a bare slug alone.
2. Experiment ids must include UTC timestamp plus a random suffix.
3. Only append to `index/experiments.tsv`; do not rewrite history in place.
4. Acquire `locks/index.lock` before touching central index files.
5. Write regenerated index output to a temp file and `rename` it into place.
6. If more than one agent will edit the same codebase, each agent must work from its own git worktree under `workspaces/<experiment-id>/`. If git worktrees are unavailable, use separate copied workspaces.
7. If the real workspace should live elsewhere, create it there and keep `workspaces/<experiment-id>/` as a symlink entry point.

## Constitution Rules

1. The skill must inherit and obey any parent constitution, project policy, or user task constraint.
2. Before planning or writing, review both the active constitution and the notebook index.
3. If the parent rules are stricter than the skill guidance, the stricter rule wins.
4. Add local guardrails when the parent context implies them, even if they are not already written into the experiment prompt.

## Provenance Rules

1. Track labnb-managed creates and updates with append-only provenance files inside each entry directory.
2. Treat provenance as best-effort, because external changes or deletions may happen outside labnb tracking.
3. Require explicit confirmation before labnb performs deletions.
4. Make entry status explicit from the start so ideas and experiments do not silently stay in an ambiguous state.
5. Use W3C PROV-O terms for recorded provenance so the records have a standard vocabulary.
6. Derive monitored slice state from provenance snapshots instead of relying on a separate mutable state file.

## Index And Resume Rules

1. The shared index should hold both idea entries and experiment entries.
2. Ideas are for promising directions that have not yet been implemented or explored.
3. Before starting a new experiment, summarize matching entries for the project slug.
4. Use that summary to choose between resume, promote, branch, or new experiment.
5. Allow new experiments to reference one or more upstream source entries.
6. Preserve append-only history in the index even when an idea later becomes a child experiment.

## Loop Rules

1. Gather the setup gate first: goal, metric, direction, verify command, scope, stop condition, and any stated time budget.
2. Treat time budgets as ceilings rather than targets to consume.
3. Start with the smallest useful iteration that can produce information or a decision.
4. Count any proposed parallel branch or downstream follow-up against the same budget unless it is explicitly deferred.
5. Require explicit overall and loop budgets when creating an experiment.
6. Say explicitly when the budget is too small for even one useful slice.
7. Record a baseline before the first code-changing iteration.
8. Make one focused change per iteration.
9. Verify mechanically after each change.
10. Keep improved or equally good-but-simpler results; discard regressions.
11. Log every iteration locally in the experiment directory.
12. Before leaving background work unattended, either start an explicit timer/watchdog or stop with a resume checkpoint.
13. Before adding a new wait job, check whether one is already pending for the experiment and either reuse it or replace it.
14. If the loop or overall budget is exceeded, record that as `budget_exhausted` rather than a generic stop.
15. When the task compares two alternatives by a metric, including comparisons against one or more prior runs, plan the smallest asynchronous comparison path and checkpoint rather than assuming the whole loop must stay active until final conclusion.
16. If parallel follow-up experiments are delegated to subagents, count their resource usage against the same budget unless the later work is explicitly deferred.
17. Unless absolutely necessary, do not run long-lived work blindly; instrument it with enough logging, checkpoints, and periodic external checks of progress and resource consumption.

## Validation

- [x] Script creates the global layout from scratch
- [x] Script registers one experiment and emits metadata, a plan, and a per-experiment results log
- [x] Script can register an idea entry without creating an active experiment workspace
- [x] Summary helper reports existing idea and experiment entries for a project slug
- [x] Parallel registrations produce unique experiment ids
- [x] Parallel registrations append the expected number of index rows
