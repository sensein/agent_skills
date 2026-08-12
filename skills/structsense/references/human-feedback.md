# Human-in-the-loop feedback

A simple pattern for inserting a human reviewer between automated stages.

## When to enable

- High-stakes outputs (clinical, regulatory).
- Active learning / curation workflows.
- Schema is new and you don't yet trust the model's outputs.
- Domain expert wants to catch edge cases before they propagate downstream.

Skip when running unattended batch jobs.

## The contract

The human feedback stage:

- **Receives:** the judge stage's output (full JSON).
- **Asks the human:** approve / abort / edit / skip.
- **Returns:** revised JSON with the same shape, plus `human_feedback_applied: true` and a `human_feedback_log` entry.

The human **cannot rename keys, drop required fields, or change types**. They can:

- Edit string values (e.g. fix `ontology_label` typos).
- Mark items as `verified: true`.
- Add `remarks` text.
- Remove individual items they consider hallucinated.

If the human wants to add new items, they should re-run the extractor with a more specific prompt — don't paste freeform items into the JSON.

## The four-option menu

```
1. Approve and continue       — pipeline accepts the judge output verbatim
2. Abort pipeline             — pipeline returns the judge output and stops; downstream stages don't run
3. Open editor to provide feedback — opens $EDITOR with a feedback area pre-populated
4. Skip feedback for this step — same as Approve but logs "user_skipped"
```

Default to **option 4 on a 60-second timeout** for unattended runs. Don't block a 10-hour pipeline on a human who walked away.

## Editor template

When the human picks option 3, present:

```
# === WRITE YOUR FEEDBACK HERE ===
[WRITE YOUR FEEDBACK HERE]

# === OUTPUT JSON (read-only reference; do not edit below this line) ===
# {
#   "entities": [
#     ...
#   ]
# }
```

Closing without writing → return to menu (treat as "skip"). Saving with text → that text becomes `user_feedback_text`.

## How the feedback agent applies feedback

The feedback agent receives:

- `judged_structured_information`: the full judge output.
- `user_feedback_text`: the freeform feedback string from the human.
- `modification_context`: optional structured context (which items were highlighted, etc.).

Its job: emit the same JSON structure, revised according to the feedback. The system prompt should hardcode "**preserve structure; only revise according to feedback; never invent or drop items**" — see `prompts/humanfeedback.md`.

## Audit trail

Always append a `human_feedback_log` entry:

```jsonc
{
  "human_feedback_log": [
    {
      "timestamp": "2026-06-05T14:23:01Z",
      "reviewer": "tekraj@mit.edu",
      "action": "edited",                      // "approved" | "aborted" | "edited" | "skipped"
      "user_feedback_text": "Item 7 says 'mice' but the source says 'rats'.",
      "items_changed": [7],
      "model": "openrouter/openai/gpt-4o-mini" // model used to apply the feedback
    }
  ]
}
```

This makes outputs reproducible and auditable months later.

## Triggering by environment

Most setups use a single env var:

```bash
ENABLE_HUMAN_FEEDBACK=true
```

When false (default), the human feedback stage is skipped entirely and the pipeline returns the judge output.

## Direct vs agent-loop implementation

Two ways to run the feedback agent:

- **Direct API** (default): send a single LLM call with the system prompt + the judge JSON + the feedback. Fast, predictable, cheap.
- **Agent loop** (CrewAI-style): the model can call tools to re-fetch source text, look up ontologies, etc. Useful if the feedback is "verify item 7 against the original PDF", but expensive.

Default to direct API. Switch to agent loop only when the feedback explicitly requires tool calls.

## Common feedback patterns

| Feedback text | What the agent should do |
|---|---|
| "Item 7 should be label=Disease, not Phenotype." | Locate item by index, update `label`. Keep everything else. |
| "Drop the items in the references section, they're not real entities." | Remove items whose `paper_location` matches "References". Keep all others. |
| "All mentions of 'mouse' should map to NCBITaxon:10090." | Find items where `entity.lower() == "mouse"`, set `ontology_id` and friends. |
| "The whole alignment looks wrong, re-run with stricter prompts." | Don't try to apply this; pipeline operator should re-run with a different config. |

The agent should *politely refuse* feedback it can't safely apply, returning an `errors[]` entry explaining why, rather than guessing.
