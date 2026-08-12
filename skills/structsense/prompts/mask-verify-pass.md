# Mask-verify pass — label sanity check from context (boosts precision)

Cloze-style verification of an already-extracted entity. For each candidate item, replace its span with `[MASK]` and ask the model to predict the label and surface form from context alone. Disagreement with the original label signals a candidate error.

Use this **after** the extractor and (optionally) the alignment stage; before or alongside the judge. It produces a per-item `mask_label_agreement` boolean + a `mask_predicted_label` field that the judge can weight into its score.

## How it works

For each entity `e` in the extracted list:

1. Replace `text[e.start:e.end]` with `[MASK]` (preserving offsets via padding).
2. Send the resulting masked-context sentence (or paragraph) to the model.
3. Model returns: a predicted label + the most likely surface form.
4. Compare:
   - `mask_label_agreement` = (predicted_label == e.label).
   - `mask_surface_agreement` = (predicted_surface in {e.entity, …common variants})`.

Run this in parallel over all items (it's per-item batched API calls).

## System

```
You are running a verification check on a single named-entity mention.

CONTEXT
You are shown a sentence with ONE span replaced by [MASK]. The original
text at that position was extracted as an entity. Your job is to predict,
PURELY FROM CONTEXT, what should fill the [MASK]:
- the most likely entity label (one of the labels listed below)
- the most likely surface form (a short string)
- your confidence that any entity belongs at that position at all

LABEL TAXONOMY
{label_taxonomy_block}

OUTPUT
Strict JSON. No prose. No markdown fences.
{
  "predicted_label":    "<one of the labels above, or 'NotAnEntity'>",
  "predicted_surface":  "<most likely surface form, or null>",
  "is_entity_position": <true|false>,
  "context_confidence": <float 0-1>,
  "remarks":            "<one short sentence>"
}

RULES
1. Use ONLY the visible context to decide. Do not invent unrelated content.
2. If the context is too generic to identify any entity ("the [MASK] was
   observed"), set predicted_label="NotAnEntity",
   is_entity_position=false, context_confidence near 0.
3. Use the SAME label taxonomy as the extractor.
4. predicted_surface should be short (1-5 tokens), guessing what word(s)
   the [MASK] replaced.

If you cannot comply, output {"error": "<one-line reason>"}.
```

## User

```
MASKED CONTEXT (one sentence; the [MASK] is the position under verification):
<<<
{masked_sentence}
>>>

(Optional surrounding context, up to 1–2 paragraphs:)
{surrounding_context}

LABEL TAXONOMY REMINDER:
{label_taxonomy_block}
```

## Wiring it into the pipeline

```python
# Pseudocode
for item in extracted_entities:
    masked = mask_one(item, source_text)             # replace just this span
    pred = call_llm(model, MASK_VERIFY_PROMPT,
                    user={"masked_sentence": masked.sentence,
                          "surrounding_context": masked.context})
    item["mask_predicted_label"]    = pred["predicted_label"]
    item["mask_predicted_surface"]  = pred["predicted_surface"]
    item["mask_label_agreement"]    = (pred["predicted_label"] == item["label"])
    item["mask_surface_agreement"]  = surface_match(pred["predicted_surface"], item["entity"])
    item["mask_context_confidence"] = pred["context_confidence"]
```

The judge can now weight `judge_score`:

- Big down-weight if `mask_label_agreement=false` AND `mask_context_confidence > 0.7`.
- Slight down-weight if `mask_surface_agreement=false` AND the surface forms aren't paraphrases.
- Up-weight if both agree AND context confidence is high.

## When to use it

| Situation | Verdict |
|---|---|
| Cost-sensitive run | **Skip.** It's a per-item LLM call; expensive on large extractions. |
| Quality-first run on high-stakes outputs | **Run.** Catches mis-labelled entities the judge often misses. |
| Calibrating a new extractor model | **Run on a sample.** Lets you measure label-confusion patterns. |
| Verifying alignment IRIs are sensible | Use `judge.md` instead — it sees the IRI too. |

## Cost notes

For 1000 entities and a cheap model (`gpt-4o-mini`, `claude-haiku-4-5`), one verify pass is roughly the cost of 1000 short completions. With parallel concurrency=8 it finishes in 1–3 minutes. Skip on prototypes; consider running on a 5–10% sample to surface systematic label errors before paying for a full pass.
