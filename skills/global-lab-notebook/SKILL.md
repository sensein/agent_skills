---
name: global-lab-notebook
description: Create and maintain a concurrency-safe global lab notebook outside project roots, with unique experiment directories, append-only indexing, and per-experiment logs for work that spans multiple repos or tasks.
---

# Global Lab Notebook

Use this skill when the user wants a reusable experiment notebook that survives across tasks, repositories, and sessions.

The key difference from repo-local `.lab/` workflows is that the lab root lives in a general location and must stay safe under parallel use.

## Default Root

Use this location unless the user explicitly wants another path:

```bash
${LAB_NOTEBOOK_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/lab-notebook}
```

This keeps the notebook outside any single repo and outside any specific agent runtime, while still making it easy to override.

## Interactive Setup Gate

Before starting a new experiment, gather or infer these fields:

1. Goal
2. Mechanical metric
3. Direction: higher or lower is better
4. Verify command
5. Writable scope
6. Stop condition: target metric, iteration count, or manual stop

If any of the first four are missing and cannot be inferred safely, ask before starting. The notebook is global, but the experiment loop still needs a clear local objective.

## Required Guarantees

1. Never use a fixed experiment directory name.
2. Never let two runs compete to rewrite the central index without a lock.
3. Never store the only copy of experiment state inside the project being studied.
4. Never delete or rewrite prior experiment rows from the central index.
5. Prefer deterministic helpers in [`scripts/register_experiment.py`](./scripts/register_experiment.py) instead of ad hoc shell snippets.

## Lab Layout

```text
lab-notebook/
  experiments/
    <experiment-id>/
      metadata.json
      plan.md
      log.md
      results.tsv
      summary.md
      artifacts/
  index/
    experiments.tsv
    index.md
  locks/
```

- `experiments/<experiment-id>/`: one experiment per directory; no sharing across active runs
- `experiments/<experiment-id>/plan.md`: the setup gate plus current hypothesis
- `experiments/<experiment-id>/results.tsv`: local iteration ledger for this experiment
- `index/experiments.tsv`: append-only registry of experiments
- `index/index.md`: generated readable summary of known experiments
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
2. Derive a `project_slug` from the current repo or working directory name.
3. Pick an `experiment_slug` that describes the current investigation.
4. Create the experiment by running the helper:

```bash
python skills/global-lab-notebook/scripts/register_experiment.py \
  --lab-root "$LAB_ROOT" \
  --project-root "$PWD" \
  --project-slug "$PROJECT_SLUG" \
  --experiment-slug "$EXPERIMENT_SLUG" \
  --objective "Short statement of the experiment goal" \
  --metric-name "$METRIC_NAME" \
  --direction "$DIRECTION" \
  --verify-command "$VERIFY_COMMAND"
```

5. Record baseline iteration `0` in `results.tsv` before code changes.
6. Work inside the returned experiment directory for notes, artifacts, and summaries.
7. Log progress inside that experiment directory, not in shared files.

## What Goes In Each Experiment

- `metadata.json`: creation metadata, source repo path, objective, and ids
- `plan.md`: goal, metric, direction, verify command, scope, and next hypothesis
- `log.md`: chronological notes for the experiment
- `results.tsv`: one row per iteration or thought with status and metric outcome
- `summary.md`: final concise outcome
- `artifacts/`: scratch outputs, plots, reports, and temporary files worth keeping

Keep detailed notes local to the experiment directory. The global index should stay compact.

## Improvement Loop

Use the same tight loop pattern that powers autoresearch, but anchor it in the global notebook:

1. Observe: read `plan.md`, the tail of `log.md`, `results.tsv`, and relevant project state.
2. Pick one focused change. Prefer atomic edits so the outcome is explainable.
3. If tracked project files change, commit before verification so rollback is cheap.
4. Run the verify command and capture the metric.
5. Keep or discard:
   - keep when the metric improves
   - keep when the metric ties and the result is clearly simpler
   - discard or revert when the metric regresses or the run crashes
6. Log the outcome in both `results.tsv` and `log.md`.
7. Repeat until the stop condition is reached or the user interrupts.

When the work diverges materially, register a child experiment instead of overloading the current one.

## Shared Index Rules

The central index exists to answer: what experiments exist, where are they, and what are they about?

When creating or updating the shared index:

1. Acquire `locks/index.lock` using an atomic directory create.
2. Append exactly one row per new experiment to `index/experiments.tsv`.
3. Rebuild `index/index.md` from `experiments.tsv` while the lock is held.
4. Write the regenerated markdown to a temp file and rename it into place atomically.
5. Release the lock even on failure.

If a lock appears stale, only clear it after confirming the owning process is gone. Prefer waiting over forcing.

## Parallel Work Rules

1. Every active experiment gets its own directory.
2. Two agents may read the same project, but they must not share the same new experiment directory.
3. If multiple agents need related work, give each one a new experiment id and link them in notes instead of co-writing.
4. Treat `index/experiments.tsv` as append-only history.
5. If an experiment needs a follow-up, create a child experiment and record the parent id in `metadata.json` or `log.md`.

## Resume Flow

When resuming:

1. Read `index/index.md` or search `index/experiments.tsv` for the project slug.
2. Open the target experiment directory.
3. Continue writing only inside that experiment directory unless you are registering a new experiment.
4. Register a fresh experiment instead of mutating old metadata if the scope or hypothesis changed materially.

## Notes For Agents

- Prefer the helper script over handwritten lock logic when the script is available.
- If you need a quick view of the notebook, read `index/index.md` first and only inspect specific experiment directories afterward.
- Re-use the same experiment directory for iterative logging, but create a new child experiment when you need a new loop with a new hypothesis or project state.
- Keep instructions concise in user-facing updates; the notebook should do the long-term memory work.
