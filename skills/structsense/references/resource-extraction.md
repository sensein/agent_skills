# Resource extraction (tools / datasets / models / benchmarks)

## Goal

Given a paper, README, or homepage, extract **one primary research resource** with its metadata, and capture related resources only as **secondary mentions**.

This is different from NER:
- NER pulls **many spans**; resource extraction pulls **one structured record** per source.
- NER labels are entity types; resource `type` is fixed: `Model`, `Dataset`, `Tool`, `Benchmark`, `Leaderboard`, `Paper`.

## Output schema

See `schemas/resource-output.schema.json`. Top level:

```jsonc
{
  "extracted_resources": {
    "1": [{
      "name": "string",                // canonical name
      "description": "string",         // 1–3 sentences, factual
      "type": "Model | Dataset | Tool | Benchmark | Leaderboard | Paper",
      "category": "string",            // free text task category, e.g. "Pose Estimation"
      "target": "string",              // e.g. "Animal", "Human", "Multimodal"
      "specific_target": "string",     // e.g. "Quadruped, Horse, Mice"
      "url": "string",                 // canonical URL (homepage / repo)
      "mentions": {                    // OTHER resources referenced — secondary only
        "datasets": ["string"],
        "benchmarks": ["string"],
        "models": ["string"],
        "papers": ["string"]
      }
    }]
  }
}
```

The numeric key (`"1"`) is so a single batch can hold multiple resources from different sources. For a single document, you'll usually have just `"1"`.

## "One primary resource" rule

The single biggest failure mode is the model dumping every mentioned model/dataset as a top-level resource. To prevent it, encode this rule in the extractor prompt:

> Identify **the single primary resource** the source document is about. Every other tool, dataset, model, benchmark, or paper that is referenced goes under `mentions`, never as a sibling top-level resource.

Heuristics for "the primary one":

- For a paper: the artifact in the title and the abstract's "we introduce/propose/release …" sentence.
- For a README: the repo's own name and tagline.
- For a model card: the model the page documents.

## Type field discipline

`type` is a **closed vocabulary**. Reject anything outside the set:

```
Model | Dataset | Tool | Benchmark | Leaderboard | Paper
```

If the model wants to emit `"Library"` or `"Framework"`, normalize to `"Tool"`. If it wants `"Corpus"`, normalize to `"Dataset"`.

## Alignment for resources (different from NER)

For resources, alignment **doesn't** add `ontology_id` to the whole resource. Instead it adds nested concept mappings to specific fields:

```jsonc
{
  "name": "DeepLabCut SuperAnimal-Quadruped",
  "type": "Model",
  "target": "Animal",
  "mapped_target_concept": [
    { "id": "http://purl.obolibrary.org/obo/BTO_0000042",
      "label": "animal", "ontology": "BTO" }
  ],
  "specific_target": "Quadruped, Horse, Mice",
  "mapped_specific_target_concept": [
    { "specific_target": "Mice",
      "mapped_target_concept": {
        "label": "Mus musculus", "id": "NCBITaxon:10090", "ontology": "NCBITaxon"
      }
    }
  ],
  "concept_mapping_provenance": "tool"
}
```

The alignment stage:
1. Maps `target` → `mapped_target_concept` (usually one concept).
2. Splits `specific_target` on commas and maps each → `mapped_specific_target_concept` (list of `{specific_target, mapped_target_concept}`).
3. Does **not** rewrite `name`, `description`, `type`, `category`, `url`, or `mentions`.

## Judging resources

The judge scores **the primary resource only**, not items inside `mentions`. The score reflects:

- Are `name`, `type`, `url` plausible and consistent with the description?
- Is the resource correctly classified (Model vs Tool vs Dataset)?
- Does the ontology mapping make sense?

```jsonc
{
  "judge_resource": {
    "1": [{
      ...all fields preserved...
      "judge_score": 0.92,
      "remarks": "Type=Model is correct; mapped_specific_target_concept for 'Mice' is the right NCBITaxon."
    }]
  }
}
```

## What to feed the extractor

For a multi-page paper, feed:

1. Title + abstract
2. The "Introduction" paragraphs that name the artifact
3. Any "Implementation" / "Release" / "Availability" section
4. The first table that lists datasets/benchmarks used

Skip Methods/Experiments details — they're a distraction for resource extraction (they mostly belong under `mentions`).

If the source is a README, feed the README's first 2–3 sections (intro + install + usage) plus the "Citation"/"Related work" section if present.

## Worked walkthrough

For a paper introducing **DeepLabCut SuperAnimal-Quadruped**:

| Field | Value |
|---|---|
| `name` | "DeepLabCut SuperAnimal-Quadruped" |
| `type` | "Model" |
| `category` | "Pose Estimation" |
| `target` | "Animal" |
| `specific_target` | "Quadruped, Horse, Mice" |
| `url` | "https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html" |
| `mentions.datasets` | ["Quadruped-80K", "AnimalPose"] |
| `mentions.benchmarks` | ["AP-10K", "AnimalPose"] |
| `mentions.models` | ["DeepLabCut"] |

Note that **DeepLabCut** (the parent toolkit) is a mention because the *primary* resource here is the SuperAnimal-Quadruped model, not the toolkit.

## Edge cases

- **Multi-resource releases** (e.g. "we release a dataset and an accompanying model"): emit each as a separate top-level resource under `"1"`, `"2"`. Don't merge.
- **Resources without URLs**: emit `"url": null` rather than guessing. Made-up URLs poison downstream consumers.
- **Vague targets** ("various species"): set `specific_target: null`. Don't pad with examples that weren't in the text.
- **Versions** ("DeepLabCut v2.3"): keep the version in `name`; don't add a separate `version` field unless your schema asks for one.
