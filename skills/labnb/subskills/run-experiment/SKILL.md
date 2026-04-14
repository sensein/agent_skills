---
name: labnb-run-experiment
description: Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging.
---

# Run Experiment

Use this subskill when the work is concrete enough to execute now.

## Flow

1. Summarize prior notebook entries for the project.
2. Decide whether this should resume an existing run, branch from one, or start fresh.
3. Register the experiment:

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

4. Work in the dedicated workspace clone if project files change.
5. Keep the loop small:
   - smallest useful first slice
   - verify mechanically
   - log outcome
   - continue only if the checkpoint justifies it

Treat the overall budget as the cap for the whole proposed path, and the loop budget as the cap for the current slice.
