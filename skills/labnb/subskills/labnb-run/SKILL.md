---
name: labnb-run
description: Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging.
---

# Run Experiment

Use this subskill when the work is concrete enough to execute now.

## Guardrails

1. Respect any parent constitution, repo policy, or task-level write constraint already in scope.
2. Review the notebook index and the parent constitution before creating a new experiment.
3. If project files will change, work in a dedicated experiment clone or copy, not the shared source tree.
4. Never let two active experiments write to the same clone, checkout, or output directory.
5. If write scopes may overlap, separate the workspaces first and only then continue.
6. Set and review experiment status explicitly, defaulting to `started` unless a better state is known.
7. Track labnb-managed actions in provenance files, but treat that provenance as best-effort.
8. Require explicit confirmation before labnb performs deletions of artifacts, workspaces, or entry files.

## Flow

1. Review the parent constitution and repo guardrails.
2. Summarize prior notebook entries for the project.
3. Decide whether this should resume an existing run, branch from one, or start fresh.
4. Register the experiment:

```bash
python skills/labnb/scripts/register_experiment.py \
  --lab-root "$LAB_ROOT" \
  --project-root "$PWD" \
  --project-slug "$PROJECT_SLUG" \
  --experiment-slug "$EXPERIMENT_SLUG" \
  --objective "$OBJECTIVE" \
  --entry-kind experiment \
  --metric-name "$METRIC_NAME" \
  --direction "$DIRECTION" \
  --verify-command "$VERIFY_COMMAND" \
  --overall-budget "$OVERALL_BUDGET" \
  --loop-budget "$LOOP_BUDGET"
```

5. Work in the dedicated workspace clone if project files change.
6. Keep the loop small:
   - smallest useful first slice
   - verify mechanically
   - log outcome
   - continue only if the checkpoint justifies it

Treat the overall budget as the cap for the whole proposed path, and the loop budget as the cap for the current slice.
