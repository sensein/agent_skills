---
name: labnb-capture-idea
description: Record a promising but not-yet-implemented experiment idea in the global lab notebook index.
---

# Capture Idea

Use this subskill when there is a useful direction to remember, but not enough evidence, time, or scope to run it now.

## Flow

1. Summarize existing entries for the project first.
2. Choose a concise slug for the idea.
3. Register the idea:

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

4. Fill in `idea.md` with:
   - why it matters
   - what prior evidence to revisit
   - the smallest useful first slice
   - what would justify promoting it into an experiment

Ideas should be first-class index entries, not buried in an active experiment log.
