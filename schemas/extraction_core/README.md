# Extraction Core schema

`extraction_core.yaml` is a [LinkML](https://linkml.io) schema shared by every extraction type the skills in this repository produce: named entities, relations, resources, structured/ABCD records, Allen cell-type mappings, assertions and evidence, and systematic-review data. It does two things:

1. **Universal slots.** Every extracted item is an `ExtractionRecord`, and every `ExtractionRecord` carries the same core slots, so records from different skills and papers can be merged, queried and audited the same way.
2. **Provenance for reproducibility.** Every record points to the `ExtractionRun` that produced it. Each run pins the exact skill version, model version(s), prompt hashes, configuration hash, software and ontology versions it used.

Version: **0.1.0** (`schema_version` on a dataset says which version it conforms to).

## Universal slots (on every `ExtractionRecord`)

| Slot | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable, deterministic identifier (CURIE). |
| `paper_id` | **yes** | The `SourceDocument` the record came from (`prov:hadPrimarySource`). |
| `source_location` | | One or more `SourceLocation`s: section, section type, page, paragraph/sentence index, character offsets plus their `offset_convention`, sentence text, figure/table label, XML id/path. |
| `source_excerpt` | | The source text, copied verbatim so code can find it again. |
| `assertion` | | One normalized sentence stating what the record asserts. For an `Assertion`, this is the claim itself. |
| `generated_by` | **yes** | The `ExtractionRun` that produced the record (`prov:wasGeneratedBy`). |
| `extracted_by` | | The `ModelVersion` that produced this record when the run used several models, as in an NER ensemble. It must be one of the run's `models`. |
| `extracted_at` | | When the record was produced. |
| `confidence`, `confidence_method` | | Producer confidence in [0, 1], plus how it was computed. |
| `evidence_verification` | | The result of a code check of `source_excerpt` against the source: `exact`, `normalized`, `relocated`, `fuzzy`, `not_found` or `not_checked`. The values follow structsense `ait_evidence.py`. |
| `record_status` | | Where the record is in curation: `candidate`, `verified`, `accepted`, `rejected`, `quarantined` or `superseded`. |
| `reviews` | | `ReviewDecision`s from model judges or humans. Each one records the reviewer, review run, dimension, decision and score. |
| `derived_from`, `supersedes` | | Lineage to earlier records and earlier versions of this record. |
| `species_context` | | NCBITaxon CURIEs that scope the record. |

## Provenance model

```
ExtractionDataset
 ├─ skill_versions   SkillVersion    skill_name + skill_version (required), repository, commit, ontology_version
 ├─ model_versions   ModelVersion    model_identifier (required, exact id never an alias), provider, kind, revision
 ├─ people           HumanAgent      curators / reviewers
 ├─ prompts          PromptVersion   prompt_path, prompt_sha256 (required), role
 ├─ software_versions, ontology_versions
 ├─ documents        SourceDocument  = paper_id targets (DOI, PMID, PMCID, content_checksum, text_extractor …)
 ├─ extraction_runs  ExtractionRun   task_type, skill (req), models (req), prompts_used, configuration
 │                                   (config_sha256, temperature, seed, chunking, output_schema),
 │                                   software, ontology_versions_used, extraction_path, parent_run,
 │                                   started_at / ended_at, run_status
 └─ records ─ generated_by ──► ExtractionRun
      entity_mentions   EntityMention   → refers_to_entity Entity, concept_mappings, in_assertion
      entities          Entity          resolved referent shared across papers
      assertions        Assertion       claim_category, claim_stance, modality, negated, cited_source …
      evidence_items    EvidenceItem    evidence_type / ECO code, method, statistic
      evidence_relations EvidenceRelation  evidence → target_assertion, direction, mode, strength
      assertion_relations AssertionRelation  confirms / conflicts_with / refines / supersedes / related
      entity_relations  EntityRelation  subject – predicate (CURIE) – object, causal/association …
      resource_records  ResourceRecord  datasets/tools with separate identifier and version claims
      claim_clusters    ClaimCluster
```

Sub-steps such as concept mapping, judging and combining are their own `ExtractionRun`s. They point to the main run through `parent_run`. That way each judge's model and prompt hash are recorded too.

`extraction_path` says how the model was invoked: `api`, `agent_supplied_payload` (the calling agent, such as Claude Code or Codex, was the model), `host_sequential`, `local_model` or `manual`. A run with no API call still lists the model that did the work.

## What came from where

The schema merges two draft ontologies and the provenance that structsense already writes. LinkML `class_uri`, `slot_uri`, enum `meaning` and `*_mappings` keep the link to each source, so converting to RDF loses nothing.

| Source | Taken into the core |
| --- | --- |
| [Assertion-Evidence ontology](https://github.com/sensein/assertion-evidence/blob/main/ontology/assertion-evidence-ontology.ttl) (`ae:`) | `ClaimRecord` semantics: a record does not assert truth. `ExtractionRun`, `Extractor`/`LLMModel`, `TextLocation` (char offsets, paragraph/sentence index, XML id/path), `sourceExcerpt`, `extractionPromptHash`. Assertion vs reference-only (`ClaimCategory`: PaperAsserted / ReferenceOnly / SystemInferred) and `ClaimStance`. Reified `EvidenceRelation` with separate direction (For/Against/Neutral/NonProbative), mode (Supports/Contradicts/Weakens/Qualifies/Contextualizes) and ordinal strength (weak → conclusive). Assertion relations (confirms, conflictsWith, refines, supersedes, related). `ClaimCluster`, `TemporalContext`, `hasSpeciesContext`, `ConceptMapping` (mappedToCurie/URI, alignmentMethod), `normalizationFailed`. Mention specificity (vague, heterogeneous, phenotype mentions). |
| [StructSense Named Entity ontology](../../skills/structsense/default_ontology/named_entity_ontology.owl) (`ner:`) | Immutable `EntityMention` (surface form, offsets, prefix/suffix text) kept apart from its interpretation. Resolved `NamedEntity` with `normalizedEntityKey` and `refersToEntity`. Execution artifacts: `PromptArtifact` (promptHash/promptVersion), `ModelArtifact` (modelName/provider/version), `ConfigurationArtifact` (configurationHash, temperature, randomSeed, schemaVersion), `SoftwareArtifact`, `ContainerArtifact`, `OntologyVersion`. `ReviewDecision` with judge dimensions (grounding, labeling, mapping, claims, kg-keys, ensemble-combined). Relation modality, negation and directness, from `CausalModality`/`CausalDirectness`. `SourceDocument` identifiers (DOI, PMID, PMCID, checksum). |
| structsense outputs (`ner-output.schema.json`, `resource-output.schema.json`, `batch.py` `run_metadata`, `abcd_extract.py` `provenance`) | Per-item `source_model`, which became `extracted_by`. `skill_version`, `extraction_path`, `text_extractor`, `started_at`/`ended_at`. `paper_location`, which became `section`. Resource identifier/version claims. Evidence-verification outcomes. |

## Mapping existing structsense NER output

| structsense field | Extraction Core |
| --- | --- |
| `source_metadata.{paper_title, doi, source_path}` | `SourceDocument.{title, doi, source_path}` (its `id` is the `paper_id`) |
| `entities[].entity` / `label` | `EntityMention.surface_form` / `entity_category` |
| `entities[].sentence`, `start`, `end`, `paper_location` | `source_location[].{sentence_text, char_start, char_end, section}` |
| `entities[].source_model`, `source_score` | `extracted_by` (a `ModelVersion` id), `source_score` |
| `entities[].ontology_id`, `ontology_label`, `alignment_method`, `concept_mapping_provenance` | `concept_mappings[].{mapped_curie, mapped_label, alignment_method, mapping_run}` |
| `entities[].judge_score`, `remarks`, `judge_method` | `reviews[].{score, comment}` + a judging `ExtractionRun` |
| `entities[].referent_id`, `identity_key` | `refers_to_entity`, `identity_key` |
| `run_metadata.{started_at, ended_at, extractor_model, judge_model, mode}` | `ExtractionRun.{started_at, ended_at, models, extraction_path}` + child judging run |
| `causal_relations[]` | `EntityRelation` with `relation_type: causal` |
| `extracted_resources[]` | `ResourceRecord` with `identifier_claims` / `version_claims` |

## Using the schema

```bash
pip install 'linkml>=1.9,<2'

# Validate a dataset: JSON Schema checks + cross-record provenance rules
python schemas/extraction_core/check_dataset.py my_extraction.yaml

# Plain LinkML validation only
linkml-validate -s schemas/extraction_core/extraction_core.yaml -C ExtractionDataset my_extraction.yaml

# Generate artifacts
gen-json-schema schemas/extraction_core/extraction_core.yaml > extraction_core.schema.json
gen-pydantic    schemas/extraction_core/extraction_core.yaml > extraction_core.py
gen-owl         schemas/extraction_core/extraction_core.yaml > extraction_core.owl.ttl
linkml-convert  -s schemas/extraction_core/extraction_core.yaml -C ExtractionDataset -o out.ttl my_extraction.yaml
```

`check_dataset.py` adds the rules that JSON Schema cannot express:

- every reference resolves inside the dataset;
- `extracted_by` is one of the run's `models`, and a model reviewer is one of its review run's `models`;
- a record whose grounding is `not_found` cannot be `accepted`;
- an `accepted` concept mapping cannot be `model_proposed_unverified`.

## Files

- `extraction_core.yaml`: the schema.
- `check_dataset.py`: the validator, which also runs the cross-record provenance rules.
- `examples/ExtractionDataset-hu2026.yaml`: a complete valid example with mentions, an assertion, evidence, an evidential bearing, a relation, a resource, a judge review and a cluster.
- `examples/invalid/`: datasets that must fail. One is missing provenance; the other has broken references and policy violations.
- `.linkmllint.yaml`: the lint configuration used in CI.

Tests are in `tests/test_extraction_core.py`. They run in the CI job "Extraction Core schema" and are skipped locally when `linkml` is not installed.
