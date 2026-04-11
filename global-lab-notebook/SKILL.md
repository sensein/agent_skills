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
${CODEX_LAB_ROOT:-${CODEX_HOME:-$HOME/.codex}/lab-notebook}
```

This keeps the notebook outside any single repo, while still making it easy to override.

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
      log.md
      summary.md
      artifacts/
  index/
    experiments.tsv
    index.md
  locks/
```

- `experiments/<experiment-id>/`: one experiment per directory; no sharing across active runs
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
python global-lab-notebook/scripts/register_experiment.py \
  --lab-root "$LAB_ROOT" \
  --project-root "$PWD" \
  --project-slug "$PROJECT_SLUG" \
  --experiment-slug "$EXPERIMENT_SLUG" \
  --objective "Short statement of the experiment goal"
```

5. Work inside the returned experiment directory for notes, artifacts, and summaries.
6. Log progress inside that experiment directory, not in shared files.

## What Goes In Each Experiment

- `metadata.json`: creation metadata, source repo path, objective, and ids
- `log.md`: chronological notes for the experiment
- `summary.md`: final concise outcome
- `artifacts/`: scratch outputs, plots, reports, and temporary files worth keeping

Keep detailed notes local to the experiment directory. The global index should stay compact.

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

## Notes For Codex

- Prefer the helper script over handwritten lock logic when the script is available.
- If you need a quick view of the notebook, read `index/index.md` first and only inspect specific experiment directories afterward.
- Keep instructions concise in user-facing updates; the notebook should do the long-term memory work.
