---
name: labnb-resume-from-index
description: Summarize prior ideas and experiments for a project slug, then choose whether to resume, promote, branch, or start new work.
---

# Resume From Index

Use this subskill before starting new lab notebook work for a project.

## Flow

1. Resolve the lab root.
2. Derive the project slug.
3. Run:

```bash
python skills/labnb/scripts/summarize_index.py \
  --lab-root "$LAB_ROOT" \
  --project-slug "$PROJECT_SLUG"
```

4. Summarize:
   - relevant ideas
   - active experiments
   - completed experiments worth branching from
   - the best current pickup point
5. Choose one path:
   - resume an active experiment
   - promote an idea into a new experiment
   - create a child experiment from prior work
   - register a new idea or experiment

Do not skip this step when the user is asking to start or continue lab notebook work.
