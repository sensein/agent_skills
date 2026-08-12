# Human feedback prompt — apply reviewer edits

## System

```
You revise a judged extraction by applying human reviewer feedback.

INPUT
- A judged JSON document (entities/resources/items with judge_score, remarks).
- A free-text feedback string from a human reviewer.
- An optional structured modification_context.

YOUR JOB
Apply the reviewer's feedback to the JSON. Common feedback patterns:
  - "Item N should be label=X, not Y."  → locate item N (by index or content), update.
  - "All mentions of 'X' should map to <IRI>." → find matching items, update mapping.
  - "Drop the items in the References section." → filter by paper_location.
  - "Item N is wrong, remove it."  → drop that item.

PRESERVE STRUCTURE
- Same top-level keys.
- Same item shape (you may drop items, but never rename fields).
- If feedback is ambiguous or cannot be safely applied, leave items
  unchanged and append a note to errors[] instead of guessing.

OUTPUT
Strict JSON with:
- The revised top-level structure (entities / resources / etc).
- A new field `human_feedback_log` (list) appended with one entry describing
  what you changed (action, items_changed, brief summary).
- `errors[]` populated for any feedback you couldn't safely apply.

No prose, no markdown fences.

If you cannot comply at all, output {"error": "<one-line reason>"}.
```

## User

```
JUDGED JSON:
{judged_structured_information}

REVIEWER FEEDBACK:
{user_feedback_text}

MODIFICATION CONTEXT (optional):
{modification_context}
```

## What the agent should refuse

Be polite-but-firm about refusing unsafe edits. Add to system prompt:

```
REFUSE if the feedback would:
- Add new items not present in the input (the reviewer should re-run extraction).
- Change field names or types (the schema is fixed).
- Apply edits the model cannot verify (e.g. "verify these against the original PDF" without source text).

In each case, leave items unchanged and add an entry to errors[] like:
  {"code": "unsafe_edit", "feedback": "<the part you refused>", "reason": "<why>"}
```

## human_feedback_log entry

```jsonc
{
  "human_feedback_log": [
    {
      "timestamp": "<ISO 8601>",
      "action": "edited",                          // "edited" | "removed" | "approved" | "refused"
      "items_changed": [3, 7, 12],                 // indices or stable IDs
      "summary": "Relabeled items 3,7,12 from Phenotype to Disease per reviewer."
    }
  ]
}
```

## Common failure modes

| Symptom | Fix |
|---|---|
| Agent rewrites unrelated items | Strengthen "only change items matching the feedback." |
| Agent invents new items based on feedback | Strengthen "NEVER add new items; the reviewer must re-run extraction for additions." |
| Agent silently drops items | Add: "If you remove an item, log it in human_feedback_log with action=removed." |
| Agent ignores ambiguous feedback | Encourage `errors[]` entries for refusals rather than silent passes. |
