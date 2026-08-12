# Judge prompt — per-item quality scoring

## System

```
You evaluate the quality of an extraction + ontology alignment.

INPUT
A JSON document of aligned items. Each item has fields:
- a surface form (entity / term / name)
- a label / type
- ontology_id, ontology_label, ontology, concept_mapping_provenance
- a containing sentence (source context)

YOUR JOB
For each item, add:
- judge_score: float in [0, 1] — your confidence that the extraction +
  alignment is correct.
- remarks: one short sentence explaining the score.

Calibration:
  1.00  unambiguous match, ontology label matches surface form exactly
  0.85  strong match; small surface/label variation but clearly correct
  0.65  plausible; some ambiguity (homonym, low-context)
  0.40  uncertain; ontology fits the type but may be wrong concept
  0.20  likely wrong; ontology mismatch or sense-disambiguation error
  0.00  obviously wrong / hallucinated

PRESERVE every existing field. Add only judge_score and remarks.
Do not modify ontology_id, ontology_label, label, or any other field.

OUTPUT
Strict JSON with the same top-level structure as the input.
No prose, no markdown fences.

If you cannot comply, output {"error": "<one-line reason>"}.
```

## User

```
ALIGNED JSON TO JUDGE:
{aligned_structured_information}

ORIGINAL SOURCE TEXT (use for context when judging):
{source_text}
```

## Calibration anchors

Help the model calibrate by including 2–4 example items + scores in the system prompt for the specific task. For NER:

```
EXAMPLES
- {"entity":"hippocampus","label":"BrainRegion","ontology_id":"UBERON_0002421",
   "ontology_label":"hippocampal formation","sentence":"... in the hippocampus."}
  → judge_score: 0.95, remarks: "Standard anatomy mapping; concept matches surface form."

- {"entity":"BDNF","label":"Gene","ontology_id":"HGNC:1033",
   "ontology_label":"BDNF","sentence":"... BDNF mRNA levels ..."}
  → judge_score: 1.0, remarks: "Exact gene symbol; HGNC ID is canonical."

- {"entity":"mouse","label":"Species","ontology_id":"NCBITaxon:10116",
   "ontology_label":"Rattus norvegicus","sentence":"... in mouse ..."}
  → judge_score: 0.05, remarks: "Mapped to RAT instead of MOUSE; ontology mismatch."
```

These calibration examples are worth ~5× the words in raw instructions.

## Direct-API batched judging (faster + cheaper)

For large inputs, don't run the judge through an agent loop. Make per-item batched API calls in parallel:

```python
async def judge_batch(items, source_text, model, concurrency=8):
    sem = asyncio.Semaphore(concurrency)
    async def one(item):
        async with sem:
            return await call_llm_async(
                model=model,
                system=PROMPT_JUDGE,
                user={"item": item, "source": source_text},
                response_format={"type": "json_object"},
                temperature=0,
            )
    return await asyncio.gather(*[one(i) for i in items])
```

This is ~5× cheaper than an agent loop and has predictable latency.

## Auto-approve mode (skip the judge entirely)

When you trust alignment and don't need scoring:

```python
for item in flatten(aligned):
    item["judge_score"] = 1.0
    item["remarks"] = "auto-approved"
    item["judge_method"] = "auto_approved"
```

Record `judge_method: "auto_approved"` so consumers know there was no real scoring.

## Common failure modes

| Symptom | Fix |
|---|---|
| Every score is 0.5 (no signal) | Add stronger calibration examples; lower temperature. |
| Scores cluster at 1.0 | Add at least one "obviously wrong" calibration example with score 0.0. |
| Judge modifies ontology IDs | Strengthen: "PRESERVE every existing field. ADD only judge_score and remarks." Validate diff in parser. |
| Remarks are verbose paragraphs | Add char limit: "`remarks` ≤ 140 characters." |
| Judge invents new items | Add: "Output exactly the same number of items as the input." Validate count in parser. |
