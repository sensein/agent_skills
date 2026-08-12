# Worked example — research resource extraction end-to-end

Extracts the one primary resource described in a paper, with other tools/datasets/models as `mentions`.

## Source text

```
Title: SuperAnimal-Quadruped: a foundation model for quadruped pose estimation

We introduce SuperAnimal-Quadruped, a pre-trained pose-estimation model
released through the DeepLabCut Model Zoo
(https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html). The model is
trained on the Quadruped-80K dataset, which we curated from AnimalPose,
AP-10K, and additional in-house labelling. SuperAnimal-Quadruped builds on
the DeepLabCut framework and is evaluated against the AnimalPose and AP-10K
benchmarks. Target species include mice, rats, dogs, and horses.
```

## Stage 1 — extractor

System prompt: `prompts/extractor-resource.md`.

Expected output:

```jsonc
{
  "extracted_resources": {
    "1": [{
      "name": "SuperAnimal-Quadruped",
      "description": "Pre-trained pose-estimation foundation model for quadrupeds, released through the DeepLabCut Model Zoo.",
      "type": "Model",
      "category": "Pose Estimation",
      "target": "Animal",
      "specific_target": "Mice, Rats, Dogs, Horses",
      "url": "https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html",
      "mentions": {
        "datasets":   ["Quadruped-80K", "AnimalPose", "AP-10K"],
        "benchmarks": ["AnimalPose", "AP-10K"],
        "models":     ["DeepLabCut"],
        "papers":     []
      }
    }]
  },
  "task_type": "resource"
}
```

Key observations:

- The **primary** resource is the new model SuperAnimal-Quadruped, **not** the DeepLabCut framework (which is a mention).
- The dataset Quadruped-80K, although introduced in this paper too, is also placed under `mentions.datasets`. If you wanted it as a sibling top-level resource, prompt-side hint: "Emit multiple top-level resources only when the source introduces multiple primary artifacts; for this paper, the model is the primary resource."
- `specific_target` keeps the order from the source ("mice, rats, dogs, horses") for the alignment stage to split on commas.

## Stage 2 — alignment (resource variant)

Use the resource-specific alignment variant (see `prompts/alignment.md` → "Resource-specific variation"). Alignment does **not** add `ontology_id` to the resource itself — it adds `mapped_target_concept` and `mapped_specific_target_concept`.

```jsonc
{
  "name": "SuperAnimal-Quadruped",
  "type": "Model",
  "target": "Animal",
  "mapped_target_concept": [
    { "id": "http://purl.obolibrary.org/obo/BTO_0000042",
      "label": "animal", "ontology": "BTO" }
  ],
  "specific_target": "Mice, Rats, Dogs, Horses",
  "mapped_specific_target_concept": [
    { "specific_target": "Mice",
      "mapped_target_concept": {
        "id": "NCBITaxon:10090", "label": "Mus musculus", "ontology": "NCBITaxon"
      }
    },
    { "specific_target": "Rats",
      "mapped_target_concept": {
        "id": "NCBITaxon:10116", "label": "Rattus norvegicus", "ontology": "NCBITaxon"
      }
    },
    { "specific_target": "Dogs",
      "mapped_target_concept": {
        "id": "NCBITaxon:9615", "label": "Canis lupus familiaris", "ontology": "NCBITaxon"
      }
    },
    { "specific_target": "Horses",
      "mapped_target_concept": {
        "id": "NCBITaxon:9796", "label": "Equus caballus", "ontology": "NCBITaxon"
      }
    }
  ],
  "url": "https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html",
  "mentions": { "...": "..." },
  "concept_mapping_provenance": "tool",
  "alignment_method": "direct_tool_call"
}
```

`name`, `description`, `type`, `category`, `url`, and `mentions` are preserved verbatim. Only the two mapping fields are added.

## Stage 3 — judge

```jsonc
{
  "judge_resource": {
    "1": [{
      "name": "SuperAnimal-Quadruped",
      "type": "Model",
      "judge_score": 0.93,
      "remarks": "Type=Model is correct; mapped species IDs are canonical NCBITaxon. Description faithful to source.",
      "judge_method": "llm"
    }]
  }
}
```

The judge scores **only the primary resource** under `"1"`, not the items inside `mentions`.

## Running it end-to-end

```bash
python -m structsense.scripts.pipeline \
    --task resource --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper ols \
    --out result.json
```

## Failure mode walkthrough

A common bug: the model dumps every cited dataset and tool as a sibling top-level resource, leaving `mentions` empty. Symptom in the output:

```jsonc
{
  "extracted_resources": {
    "1": [{ "name": "SuperAnimal-Quadruped", "mentions": {} }],
    "2": [{ "name": "DeepLabCut", "mentions": {} }],
    "3": [{ "name": "AnimalPose", "mentions": {} }]
  }
}
```

Fix: strengthen the extractor prompt's "primary resource" rule (see `prompts/extractor-resource.md` → "Common failure modes"). Reject and reprompt when `len(extracted_resources) > 1` and `mentions` is empty across all of them — that's the heuristic signature of this bug.
