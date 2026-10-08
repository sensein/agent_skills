# Worked example — resource extraction → resource knowledge graph

A paper introducing one model, extracted with `prompts/extractor-resource.md` and
delivered as a resource KG in the BrainKB Resource Ontology
(`default_ontology/brainkb_resource_ontology.owl`). Files:
`resource-superanimal.txt` (the source) and `resource-superanimal.json` (the
extraction, written by hand to the prompt's rules — not an observed LLM output).

## Source text

```
Title: SuperAnimal-Quadruped: a foundation model for quadruped pose estimation

We introduce SuperAnimal-Quadruped, a pre-trained pose-estimation model released through
the DeepLabCut Model Zoo (https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html). The
model is trained on the Quadruped-80K dataset, which we curated from AnimalPose, AP-10K,
and additional in-house labelling. SuperAnimal-Quadruped builds on the DeepLabCut framework
and is evaluated against the AnimalPose and AP-10K benchmarks, where it reaches 84.1 mAP on
AP-10K. Target species include mice, rats, dogs, and horses. The model expects RGB video
frames as input and outputs 39 keypoints per animal. Performance degrades on heavily
occluded animals, where keypoints can be placed on the wrong limb without any warning. The
model has not been tested on birds.
```

## Stage 1 — extraction (BKR profile)

One deep record; the datasets, benchmarks and toolkit it only names are `mentions`.
The scope is split four ways, and every filled field has a verbatim quote:

```jsonc
{"extracted_resources": [{
  "record_id": "r1", "extracted_type": "model", "name": "SuperAnimal-Quadruped",
  "url": "https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html",
  "tasks": [{"label": "pose-estimation"}],
  "applicability": {
    "declared":     [{"statement": "Target species include mice, rats, dogs, and horses.",
                      "species": [{"label": "mice"}, {"label": "rats"}, {"label": "dogs"}, {"label": "horses"}],
                      "evidence": [{"quote": "Target species include mice, rats, dogs, and horses."}]}],
    "validated":    [{"statement": "Evaluated on the AP-10K benchmark.", "evidence_level": "benchmarked",
                      "evidence": [{"quote": "where it reaches 84.1 mAP on AP-10K"}]}],
    "out_of_scope": [{"statement": "Not tested on birds.", "species": [{"label": "birds"}],
                      "evidence": [{"quote": "The model has not been tested on birds."}]}]},
  "failure_modes": [{"statement": "Keypoints can be placed on the wrong limb for heavily occluded animals.",
                     "condition": "heavily occluded animals", "silent": true, "severity": "moderate",
                     "evidence": [{"quote": "Performance degrades on heavily occluded animals, ..."}]}],
  "benchmark_evidence": [{"benchmark": "AP-10K", "metric": "mAP", "value": 84.1, "evidence": [...]}],
  "inputs":  [{"name": "video frames", "format": "RGB video frames"}],
  "outputs": [{"name": "keypoints", "description": "39 keypoints per animal"}],
  "mentions": [{"name": "Quadruped-80K", "extracted_type": "dataset"}, {"name": "AnimalPose", ...},
               {"name": "AP-10K", ...}, {"name": "DeepLabCut", "extracted_type": "software_library"}],
  "not_found_fields": ["stable_identifiers", "versions", "license", "access", "owners"],
  "field_completeness": 0.55
}]}
```

Note what is **not** there: no species IRIs (rule 4 — labels only), no version (the
paper states none), no licence (the paper is silent; "open" would be invented).

## Stage 2 — resource KG

```bash
python -m scripts.resource_kg build examples/resource-superanimal.json \
    --source examples/resource-superanimal.txt --map --out superanimal.ttl
python -m scripts.validate_ttl superanimal.ttl
```

Result (trusted ontologies indexed): 284 triples, 1 resource, 1 record, 5 tool-backed
mapping decisions (all accepted, each with a SKOS shortcut; `tasks` is not routed to a
mapper and stays a label), 7 evidence quotes, nothing removed by grounding; the
`AP-10K` mention merges onto the benchmark node the benchmark result created, the other
three stay stubs. The gate passes with 0 violations and 4 **source-silence findings**:
three citable nodes state no licence, and the `DeepLabCut` stub declares no scope (it is
named, never described).

The scope split, as queried from the graph:

| scope | statement | taxon |
|---|---|---|
| declared | Target species include mice, rats, dogs, and horses. | NCBITaxon_10090, _10116, _9615, _9788 |
| declared | unmapped tasks: pose-estimation | — |
| validated | Evaluated on the AP-10K benchmark. (`bkr:scopeValidatedBy` → the reported evidence) | — |
| out of scope | Not tested on birds. | NCBITaxon_8782 |

The failure mode is a `bkr:FailureMode` with `bkr:isSilentFailure true`; the benchmark
result a `bkr:BenchmarkResult` / `dqv:QualityMeasurement` (`mAP` 84.1 on the `AP-10K`
benchmark node). Every resource is `dcterms:isReferencedBy` the paper's
`ner:Publication`, and the record carries `bkr:notFoundField` for each gap.

## What grounding would have removed

Had the extractor written a version (`"versions": [{"version": "3.0"}]`), an RRID, a
DOI from memory or a paraphrased quote, `resource_kg` would have removed each one,
added the field to `not_found_fields` and listed it in the report
(`grounding.removed`) — the KG never states what the paper does not.

## Querying

```bash
python cqs/run_cqs.py superanimal.ttl --cqs cqs/brainkb_resource_ontology_CQs.md \
    --with-ontology default_ontology/brainkb_resource_ontology.owl --entail
```

CQ02 (declared vs validated vs out-of-scope taxa), CQ17 (silent failure modes), CQ19
(attesting works) and CQ10 (mapping audit) answer directly from this one paper.

## Legacy input

The earlier shape (`{"extracted_resources": {"1": [{name, type: "Model", category,
target, specific_target, url, mentions: {datasets, benchmarks, models, papers}}]}}`)
still converts — through the crosswalk the ontology declares (`bkr:structsenseField`) —
to the same kind of resource KG; see `references/resource-extraction.md` → Legacy input.
