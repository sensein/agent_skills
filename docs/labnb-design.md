# LabNB Skill Design

## Goal

Create a named skill, `labnb`, that adapts the repo-local `.lab/` workflow into a global lab notebook that:

- lives outside project trees
- tracks experiments across unrelated repos and tasks
- tracks not-yet-run experiment ideas alongside active or completed experiments
- keeps a simple human-readable experiment index
- avoids conflicting writes when multiple experiments run in parallel
- lives under `skills/` in this repository so it can be mirrored into agent skill directories easily
- is broken into focused subskills for index resume, idea capture, and active experiment execution

## Decisions

- [x] Use an agent-neutral global root: `${LAB_NOTEBOOK_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/lab-notebook}`
- [x] Give every experiment a unique immutable directory created atomically
- [x] Keep the global index append-only and update it under a lock
- [x] Regenerate a readable index file with atomic rename while holding the same lock
- [x] Prefer deterministic helper scripts for directory creation and index updates
- [x] Bring over the `observe -> modify -> verify -> keep/discard -> log -> repeat` loop from `uditgoenka/autoresearch`
- [x] Give each experiment its own workspace path so parallel agents never share a git clone
- [x] Support linking the workspace to an external location while keeping a stable notebook path
- [x] Add a concurrency test that proves parallel registrations do not collide
- [x] Treat user time budgets as ceilings and require a smallest-useful-iteration plan with an explicit infeasibility path
- [x] Apply the same budget realism to proposed parallel branches and downstream child experiments
- [x] Allow an optional loop budget to be recorded directly in experiment registration metadata and plan scaffolding
- [x] Split overall experiment budget from loop budget so the total plan cap is explicit
- [x] Allow the index to hold both ideas and experiments, with a summary step before starting new work

## Layout

```text
lab-notebook/
  ideas/
    <idea-id>/
      metadata.json
      idea.md
  experiments/
    <experiment-id>/
      metadata.json
      plan.md
      log.md
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
6. If more than one agent will edit the same codebase, each agent must work from its own clone path under `workspaces/<experiment-id>/`.
7. If the real workspace should live elsewhere, create it there and keep `workspaces/<experiment-id>/` as a symlink entry point.

## Index And Resume Rules

1. The shared index should hold both idea entries and experiment entries.
2. Ideas are for promising directions that have not yet been implemented or explored.
3. Before starting a new experiment, summarize matching entries for the project slug.
4. Use that summary to choose between resume, promote, branch, or new experiment.
5. Preserve append-only history in the index even when an idea later becomes a child experiment.

## Loop Rules

1. Gather the setup gate first: goal, metric, direction, verify command, scope, stop condition, and any stated time budget.
2. Treat time budgets as ceilings rather than targets to consume.
3. Start with the smallest useful iteration that can produce information or a decision.
4. Count any proposed parallel branch or downstream follow-up against the same budget unless it is explicitly deferred.
5. Say explicitly when the budget is too small for even one useful slice.
6. Record a baseline before the first code-changing iteration.
7. Make one focused change per iteration.
8. Verify mechanically after each change.
9. Keep improved or equally good-but-simpler results; discard regressions.
10. Log every iteration locally in the experiment directory.

## Validation

- [x] Script creates the global layout from scratch
- [x] Script registers one experiment and emits metadata, a plan, and a per-experiment results log
- [x] Script can register an idea entry without creating an active experiment workspace
- [x] Summary helper reports existing idea and experiment entries for a project slug
- [x] Parallel registrations produce unique experiment ids
- [x] Parallel registrations append the expected number of index rows
