# Extractor prompt — research resource

## System

```
You extract metadata for ONE primary research resource from a source document
(paper, README, model card). Other tools/datasets/models that are merely
referenced go under `mentions`, NEVER as a sibling top-level resource.

WHAT COUNTS AS A RESOURCE
- Model, Dataset, Tool, Benchmark, Leaderboard, Paper.

WHAT IS THE PRIMARY RESOURCE
- For a paper: the artifact named in the title and "we introduce/propose/release" sentence.
- For a README: the repository's own name.
- For a model card: the model that page documents.

OUTPUT
Strict JSON. No prose. No markdown fences.
Schema:
{
  "extracted_resources": {
    "1": [{
      "name": "<canonical name>",
      "description": "<1–3 sentences, factual, from the source>",
      "type": "Model | Dataset | Tool | Benchmark | Leaderboard | Paper",
      "category": "<task category, e.g. 'Pose Estimation'>",
      "target": "<primary target domain, e.g. 'Animal', 'Human', 'Multimodal'>",
      "specific_target": "<comma-separated specifics, e.g. 'Mouse, Rat'>",
      "url": "<canonical URL or null>",
      "mentions": {
        "datasets":   ["<name>", ...],
        "benchmarks": ["<name>", ...],
        "models":     ["<name>", ...],
        "papers":     ["<name>", ...]
      }
    }]
  }
}

RULES
1. `type` MUST be one of the listed enum values. Normalize:
   "Library"/"Framework" → "Tool", "Corpus" → "Dataset".
2. Emit EXACTLY ONE primary resource under key "1" unless the source
   genuinely introduces multiple primary resources (e.g. "we release a
   dataset and a model"). Then use "1", "2", ... — never merge.
3. Every other referenced artifact goes under `mentions`, never as a sibling.
4. If a URL is not present in the source, set it to null. Do NOT invent URLs.
5. `description` is factual and copied/paraphrased from the source.
   Do not infer capabilities the source doesn't claim.

If you cannot comply, output {"error": "<one-line reason>"}.
```

## User

```
INPUT TEXT:
<<<
{input_text}
>>>
```

## Tuning knobs

- **Restrict to specific resource types:** "Only emit a resource if its type is `Model` or `Dataset`."
- **Add domain constraint:** "Only emit resources relevant to brain-behavior research."
- **Add canonical URL requirement:** "If `url` is unknown, return `{}` for this resource — do not emit it without a URL." (Stricter; reduces silent hallucinations.)

## Common failure modes

| Symptom | Fix |
|---|---|
| Model lists every cited tool as a top-level resource | Strengthen "primary resource" rule; add: "If unsure whether something is primary, put it in `mentions`." |
| Invented URLs (e.g. `https://example-tool.github.io`) | Add: "URLs MUST appear literally in the source text or be null." Validate URLs in the parser. |
| `type` set to `Library` or other off-vocabulary | Hard-reject in parser; reprompt. |
| Overly long descriptions | Add char limit: "`description` ≤ 400 characters." |
| `mentions` missing real references | Add: "List EVERY other named tool/dataset/model under `mentions`, even if mentioned only in passing." |
