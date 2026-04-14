---
name: labnb-idea
description: Record a promising but not-yet-implemented experiment idea in the global lab notebook index.
---

# Capture Idea

Use this subskill when there is a useful direction to remember, but not enough evidence, time, or scope to run it now.

## Guardrails

1. Respect any parent constitution, repo policy, or task-level write constraint already in scope.
2. Review the notebook index first so the idea does not duplicate an existing entry unnecessarily.
3. Capture speculative directions as ideas instead of opening active experiments that would encourage unsafe or premature writes.
4. Record idea status explicitly, defaulting to `ideation` unless a better status is known.
5. Treat provenance as best-effort; deletions still require explicit confirmation.

## Flow

1. Review the parent constitution and repo guardrails.
2. Summarize existing entries for the project first.
3. Choose a concise slug for the idea.
4. Register the idea:

```bash
python skills/labnb/scripts/register_experiment.py \
  --lab-root "$LAB_ROOT" \
  --project-slug "$PROJECT_SLUG" \
  --experiment-slug "$IDEA_SLUG" \
  --objective "$OBJECTIVE" \
  --entry-kind idea \
  --overall-budget "$OVERALL_BUDGET" \
  --loop-budget "$LOOP_BUDGET"
```

5. Fill in `idea.md` with:
   - why it matters
   - what prior evidence to revisit
   - the smallest useful first slice
   - what would justify promoting it into an experiment

Ideas should be first-class index entries, not buried in an active experiment log.
