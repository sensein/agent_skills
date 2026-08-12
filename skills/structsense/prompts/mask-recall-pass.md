# Mask-recall pass — find missed mentions (boosts recall)

A second-pass prompt that runs **after** any of the three extractor prompts (`extractor-ner-general.md`, `extractor-ner-neuroscience.md`, `extractor-ner-cns-cells.md`). Its only job: find mentions the first pass missed.

## How it works

1. Run any extractor prompt → pass-1 entities `E1`.
2. **Mask** every span in `E1` in the source text: replace `text[start:end]` with a placeholder like `[E0]`, `[E1]`, …, preserving character offsets via padding.
3. Feed the masked text back to the model with this prompt.
4. The model returns NEW mentions (anything still visible that should have been extracted).
5. Append the new mentions to `E1` and run span validation.

This is the cheapest, most reliable recall booster for NER. Use the same model as pass-1 or a smaller cheap model — even Haiku/gpt-4o-mini handles this well.

## System

```
You are running a SECOND-PASS recall check on a named-entity extraction.

CONTEXT
A first pass already extracted some entities. Those mentions have been
REPLACED in the input text with placeholder tokens of the form [E0],
[E1], [E2], … — one per mention previously extracted. Treat every [En]
token as already-extracted; do NOT re-emit it.

YOUR JOB
Identify entity mentions that the first pass MISSED — anything still
visible as natural text in the input that should have been extracted
under the same label taxonomy as the first pass.

LABEL TAXONOMY
{label_taxonomy_block}

OUTPUT
Strict JSON. No prose. No markdown fences. Schema:
{
  "missed_entities": [
    {
      "entity": "<surface form, EXACTLY as in the input>",
      "label":  "<one of the labels above>",
      "sentence": "<containing sentence as it appears in the input,
                    with [En] tokens preserved if any are inside>",
      "start": <int char offset in the MASKED input>,
      "end":   <int char offset (exclusive) in the MASKED input>,
      "missed_reason": "<short string: e.g. 'unfamiliar acronym',
                        'plural form', 'in a table', 'low-frequency surface form'>"
    }
  ],
  "missed_key_terms": [
    {
      "term": "<surface form>",
      "sentence": "<containing sentence>",
      "start": <int>, "end": <int>,
      "missed_reason": "<short>"
    }
  ]
}

RULES
1. EXHAUSTIVE: emit EVERY missed occurrence, not one per unique surface form.
   If the same missed term appears 10 times in the masked text, emit 10 items
   with 10 distinct start/end pairs.
2. start/end are offsets into the MASKED text (the input you see, with [En]
   placeholders). The post-processor will translate back to original offsets.
3. NEVER emit a [En] placeholder as an entity. NEVER produce an item whose
   `entity` field starts with '[E' and ends with ']'.
4. Use the SAME label taxonomy as pass-1.
5. If nothing was missed, return {"missed_entities": [], "missed_key_terms": []}.

If you cannot comply, output {"error": "<one-line reason>"}.
```

## User

```
MASKED INPUT TEXT:
<<<
{masked_text}
>>>

WHAT THE PLACEHOLDERS REPLACED (for context only — do not re-emit these):
{placeholder_map_json}

LABEL TAXONOMY REMINDER:
{label_taxonomy_block}
```

## How to fill the placeholders

- `{label_taxonomy_block}`: paste the LABEL TAXONOMY block from the pass-1 prompt that was used. Keep it identical so pass-2 uses the same labels.
- `{masked_text}`: source text with each pass-1 entity span replaced by `[E<i>]`. Use `scripts/mask_pass.py` to build this.
- `{placeholder_map_json}`: `[{"placeholder": "[E0]", "entity": "BDNF", "label": "Gene"}, …]` so the model knows what it's not supposed to re-extract.

## Merge back into pass-1 results

After receiving the response:

1. Translate masked-text offsets → original-text offsets (the helper in `scripts/mask_pass.py` does this).
2. Run span validation (`scripts/span_validator.py`) on each missed entity.
3. Append survivors to the pass-1 list. **Do not deduplicate by surface form** — the whole point is to keep every occurrence.
4. Re-run the alignment / judge stages on the merged result (or only on the appended subset, then merge mappings back).

## When to use it

| Situation | Verdict |
|---|---|
| Pass-1 yield feels too low (e.g. 230 entities where you expected ~1000) | **Always run.** Typical recovery: 20–60% more mentions. |
| You're on a tight budget | Skip; pass-1 alone is fine for prototypes. |
| Pass-1 already extracted ≥ 80% of expected mentions | Optional; expect <10% additional recovery. |
| You changed the label taxonomy mid-run | **Re-extract from scratch.** Mask-recall assumes the same taxonomy. |

## When NOT to chain a third pass

Stop at pass-2. Three passes give diminishing returns and start surfacing genuinely junk mentions. If recall is still insufficient after pass-2, the right fix is a better extractor model or smaller chunks, not more mask passes.
