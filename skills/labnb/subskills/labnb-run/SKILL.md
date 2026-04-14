---
name: labnb-run
description: Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging.
---

# Run Experiment

Use this subskill when the work is concrete enough to execute now.

## Guardrails

1. Respect any parent constitution, project policy, or task-level write constraint already in scope.
2. Review the notebook index and the parent constitution before creating a new experiment.
3. If project files will change, work in a dedicated experiment clone or copy, not the shared source tree.
4. Never let two active experiments write to the same clone, checkout, or output directory.
5. If write scopes may overlap, separate the workspaces first and only then continue.
6. Set and review experiment status explicitly, defaulting to `started` unless a better state is known.
7. Track labnb-managed actions in provenance files, but treat that provenance as best-effort.
8. Require explicit confirmation before labnb performs deletions of artifacts, workspaces, or entry files.
9. When writing provenance, use W3C PROV-O terms instead of ad hoc event keys.
10. Use provenance as the source of truth for monitored slice state.

## Flow

1. Review the parent constitution and local project guardrails.
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
  --loop-budget "$LOOP_BUDGET" \
  --source-id "$SOURCE_ENTRY_ID"
```

5. Budgets are required at experiment creation time; do not leave them implicit.
6. Repeat `--source-id` when the run stems from multiple ideas or previous experiments.
7. Start the monitored slice:

```bash
python skills/labnb/scripts/monitor_slice.py start \
  --experiment-dir "$EXPERIMENT_DIR"
```

8. Work in the dedicated workspace clone or copy if source files change.
9. Keep the loop small:
   - smallest useful first slice
   - verify mechanically
   - log outcome
   - continue only if the checkpoint justifies it
10. Before leaving any background command unattended, run `monitor_slice.py check` and decide whether a timer or watchdog should be started within the remaining budget.
11. Before scheduling a new wait job, check whether this experiment already has a pending wait:
   - if the earlier wait still covers the needed follow-up, do not submit a duplicate; just wait on it
   - if the new wait supersedes the older one, cancel or replace the earlier wait first
   - do not leave overlapping waits for the same experiment unless you record why in `log.md`
12. If no timer or watchdog is appropriate or available, stop deliberately instead of leaving the run hanging:
   - `python skills/labnb/scripts/monitor_slice.py finish --experiment-dir "$EXPERIMENT_DIR" --final-status stopped`
   - write a resume checkpoint in `log.md`
   - note what command or verification step to restart on resume
13. Run `monitor_slice.py check` before continuing and `monitor_slice.py finish` when the slice ends.

Treat the overall budget as the cap for the whole proposed path, and the loop budget as the cap for the current slice. If the budget is exceeded, prefer the explicit status `budget_exhausted` and record the safest next resume point.
