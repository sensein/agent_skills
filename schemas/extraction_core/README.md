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
| `source_location` | | One or more `SourceLocation`s: which `SourceFile` (main text, a supplement…), pipeline `chunk_id`, section, section type, page, paragraph/sentence index, character offsets plus their `offset_convention`, sentence text, figure/table label, XML id/path. |
| `source_excerpt` | | The source text, copied verbatim so code can find it again. |
| `assertion` | | One normalized sentence stating what the record asserts. For an `Assertion`, this is the claim itself. |
| `generated_by` | **yes** | The `ExtractionRun` that produced the record (`prov:wasGeneratedBy`). |
| `extracted_by` | | The `ModelVersion` that produced this record when the run used several models, as in an NER ensemble. It must be one of the run's `models`. |
| `extracted_at` | | When the record was produced. |
| `confidence`, `confidence_method` | | Producer confidence in [0, 1], plus how it was computed. |
| `evidence_verification`, `evidence_similarity` | | The result of a code check of `source_excerpt` against the source (`exact`, `exact_offset_corrected`, `fuzzy`, `not_found`, `not_checked`, the same values structsense `ait_evidence.py` writes) and its similarity score. |
| `claim_category` | | Whether the source states the content (`paper_asserted`), only cites it (`reference_only`), or the extractor inferred it (`system_inferred`). It is required on `Assertion`. |
| `extraction_flags` | | Quality flags: `ok`, `uncertain`, `unverified_evidence`, `generic_phrase`, `duplicate_merged`. |
| `field_provenance` | | Per-field list of the procedures that produced or checked each value (e.g. `surface_form`: `llm_extract`, `code_lexical_verify`). |
| `record_status` | | Where the record is in curation: `candidate`, `verified`, `accepted`, `rejected`, `quarantined` or `superseded`. |
| `reviews` | | `ReviewDecision`s from model judges or humans. Each one records the reviewer, review run, dimension, decision and score. |
| `derived_from`, `supersedes` | | Lineage to earlier records and earlier versions of this record. |
| `species_context` | | `SpeciesContext` entries: NCBITaxon CURIE, label, and `species_role` (`experimental`, `antibody_host`, `citation_only`, `reference_dataset`). Only experimental species scope a finding. |

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
      entities          Entity          resolved referent shared across papers; accepted matches as
                                        SKOS links (exact_match, close_match, broad_match, …)
      assertions        Assertion       claim_category, claim_stance, modality, negated, cited_source …
      evidence_items    EvidenceItem    evidence_type / ECO code, method, statistic
      evidence_relations EvidenceRelation  evidence → target_assertion, direction, mode, strength
      assertion_relations AssertionRelation  confirms / conflicts_with / refines / supersedes / related
      entity_relations  EntityRelation  subject – predicate (CURIE) – object, causal/association …
      resource_records  ResourceRecord  datasets/tools with separate identifier and version claims
      claim_clusters    ClaimCluster
```

**What each hash covers.** All three are written as `sha256:<64 hex>`, the form structsense uses; bare hex is also accepted. None of them include timestamps, run ids or file paths, so identical inputs always give identical hashes. A run is told apart from another by its own `id`, `started_at` and `ended_at`.

| Hash | Computed over |
| --- | --- |
| `SourceDocument.content_checksum` | The UTF-8 bytes of the extracted text the pipeline read. Character offsets refer to this text. |
| `PromptVersion.prompt_sha256` | The prompt file's bytes (structsense `judge_combine._file_sha256`). |
| `RunConfiguration.config_sha256` | Each configuration file's repo-relative name and bytes, in sorted order (structsense `json_to_ttl.config_hash`). |

Sub-steps such as concept mapping, judging and combining are their own `ExtractionRun`s. They point to the main run through `parent_run`. That way each judge's model and prompt hash are recorded too.

`extraction_path` says how the model was invoked: `api`, `agent_supplied_payload` (the calling agent, such as Claude Code or Codex, was the model), `host_sequential`, `local_model` or `manual`. A run with no API call still lists the model that did the work.

## Linked data: links to external ontologies

Extraction Core data should join onto the ontologies it cites, so ontology terms are written as IRI nodes, not strings.

**Accepted matches are SKOS links on the entity.** `Entity` uses the `OntologyMappable` mixin, which has the same slots as the [BICAN SKOS schema](https://github.com/brain-bican/models/blob/main/linkml-schema/bican_skos.yaml): `exact_match`, `close_match`, `broad_match`, `narrow_match`, `related_match`, and `mapping_relation` for a match of unspecified kind. They hold **accepted** matches only. `concept_mappings` stays the record of how each match was decided (candidates, rejected ones, scores, method, mapper), so a proposed or rejected match never becomes a triple.

```yaml
entities:
  - id: ex:entity/pvalb-interneuron
    exact_match: [CL:4023018]          # accepted result
    concept_mappings:                  # how it was decided
      - mapped_curie: CL:4023018
        skos_relation: exact_match
        mapping_status: accepted
        mapping_score: 0.93
```

This produces one direct triple:

```turtle
ex:entity/pvalb-interneuron skos:exactMatch <http://purl.obolibrary.org/obo/CL_4023018> .
```

`check_dataset.py` keeps the two in step. Every accepted mapping must appear in the SKOS slot for its `skos_relation`, and a SKOS slot may not hold a term without an accepted mapping behind it.

**Ontology-valued fields are nodes.** `taxon`, `entity_class`, `evidence_code`, `nearest_ancestor`, `mapped_curie` and the SKOS slots have range `OntologyClass` (an `owl:Class`), and `predicate` has range `OntologyProperty` (an `rdf:Property`). These classes are never defined in a dataset; they only make the value an IRI (`"@type": "@id"` in JSON-LD). In YAML the values are still plain CURIEs such as `NCBITaxon:10090`.

**JSON-LD context.** [`extraction_core.context.jsonld`](./extraction_core.context.jsonld) is generated from the schema by `gen_jsonld_context.py` and declares `"@version": 1.1`. OBO prefixes end in `_` (`CL_`), so the context marks them `"@prefix": true`, a JSON-LD 1.1 keyword. Without `@version: 1.1`, rdflib before 7.1 does not expand `CL:4023018` to `http://purl.obolibrary.org/obo/CL_4023018`. CI fails if the committed context is out of date, and a test loads the example as JSON-LD and checks the links.

## What came from where

The schema merges two draft ontologies and the provenance that structsense already writes. LinkML `class_uri`, `slot_uri`, enum `meaning` and `*_mappings` keep the link to each source, so converting to RDF loses nothing.

| Source | Taken into the core |
| --- | --- |
| [Assertion-Evidence ontology](https://github.com/sensein/assertion-evidence/blob/main/ontology/assertion-evidence-ontology.ttl) (`ae:`) | `ClaimRecord` semantics: a record does not assert truth. `ExtractionRun`, `Extractor`/`LLMModel`, `TextLocation` (char offsets, paragraph/sentence index, XML id/path), `sourceExcerpt`, `extractionPromptHash`. Assertion vs reference-only (`ClaimCategory`: PaperAsserted / ReferenceOnly / SystemInferred) and `ClaimStance`. Reified `EvidenceRelation` with separate direction (For/Against/Neutral/NonProbative), mode (Supports/Contradicts/Weakens/Qualifies/Contextualizes) and ordinal strength (weak → conclusive). Assertion relations (confirms, conflictsWith, refines, supersedes, related). `ClaimCluster`, `TemporalContext`, `hasSpeciesContext`, `ConceptMapping` (mappedToCurie/URI, alignmentMethod), `normalizationFailed`. Mention specificity (vague, heterogeneous, phenotype mentions). |
| [StructSense Named Entity ontology](../../skills/structsense/default_ontology/named_entity_ontology.owl) (`ner:`) | Immutable `EntityMention` (surface form, offsets, prefix/suffix text) kept apart from its interpretation. Resolved `NamedEntity` with `normalizedEntityKey` and `refersToEntity`. Execution artifacts: `PromptArtifact` (promptHash/promptVersion), `ModelArtifact` (modelName/provider/version), `ConfigurationArtifact` (configurationHash, temperature, randomSeed, schemaVersion), `SoftwareArtifact`, `ContainerArtifact`, `OntologyVersion`. `ReviewDecision` with judge dimensions (grounding, labeling, mapping, claims, kg-keys, ensemble-combined). Relation modality, negation and directness, from `CausalModality`/`CausalDirectness`. `SourceDocument` identifiers (DOI, PMID, PMCID, checksum). |
| [CellTypeExtraction & AITMapping column schema](../../skills/structsense/schemas/ait-mapping-columns.json) (the structsense AIT CSV contract) | Field-level provenance (`field_provenance` and its label vocabulary). Evidence-check outcomes (`exact_offset_corrected`) and `evidence_similarity`. `extraction_flags`. Asserted vs inferred, which became `claim_category` on every record. `species_role`. Pipeline `chunk_id`. Source files and deposits with retrieval status (`sources.csv`). Paper `license`, `abstract`, `corresponding_authors`, `full_text_source`. For concept mappings: `match_confidence` derived from the SKOS relation, `basis_for_match`, `mapping_evidence`, explicit `no_match` with `no_match_reason` and `nearest_ancestor`, `mapping_id`/`supersedes_mapping`, and `mapper_type` (software/llm/human; the AIT value `code` is stored as `software`). |
| structsense outputs (`ner-output.schema.json`, `resource-output.schema.json`, `batch.py` `run_metadata`, `abcd_extract.py` `provenance`) | Per-item `source_model`, which became `extracted_by`. `skill_version`, `extraction_path`, `text_extractor`, `started_at`/`ended_at`. `paper_location`, which became `section`. Resource identifier/version claims. Evidence-verification outcomes. |

## Mapping existing structsense NER output

| structsense field | Extraction Core |
| --- | --- |
| `source_metadata.{paper_title, doi, source_path}` | `SourceDocument.{title, doi, source_path}` (its `id` is the `paper_id`) |
| `entities[].entity` / `label` | `EntityMention.surface_form` / `entity_category` |
| `entities[].sentence`, `start`, `end`, `paper_location` | `source_location[].{sentence_text, char_start, char_end, section}` |
| `entities[].source_model`, `source_score` | `extracted_by` (a `ModelVersion` id), `source_score` |
| `entities[].ontology_id`, `ontology_label`, `alignment_method`, `concept_mapping_provenance` | `concept_mappings[].{mapped_curie, mapped_label, alignment_method, mapping_run}`; when accepted, also the entity's SKOS slot (e.g. `exact_match`) |
| `entities[].judge_score`, `remarks`, `judge_method` | `reviews[].{score, comment}` + a judging `ExtractionRun` |
| `entities[].referent_id`, `identity_key` | `refers_to_entity`, `identity_key` |
| `run_metadata.{started_at, ended_at, extractor_model, judge_model, mode}` | `ExtractionRun.{started_at, ended_at, models, extraction_path}` + child judging run |
| `causal_relations[]` | `EntityRelation` with `relation_type: causal` |
| `extracted_resources[]` | `ResourceRecord` with `identifier_claims` / `version_claims` |

## Mapping the structsense AIT tables

The cell-type AIT mode (`prompts/extractor-cell-type-ait-mapping.md`) writes seven CSVs. Its shared columns map onto the core like this:

| AIT column(s) | Extraction Core |
| --- | --- |
| `paper.csv`: `paper_id, doi, pmid, pmcid, title, journal, publication_date, abstract, license, full_text_source` | `SourceDocument` (first/last author go in the ordered `authors`; corresponding author in `corresponding_authors`) |
| `paper.csv` / every table: `run_id`, `extractor_version`, `extracted_at` | `generated_by` → `ExtractionRun` (whose `skill` holds the version), `extracted_at` |
| `sources.csv`: `source_id, source_type, identifier, url, retrieved, retrieval_status, notes` | `SourceDocument.source_files[]` (`SourceFile`) |
| `entities.csv`: `mention_id, label_verbatim, label_normalized, entity_type, aliases, n_mentions, merged_mention_ids` | `EntityMention.{id, surface_form, normalized_form, entity_category}`, `Entity.{aliases, mention_count}`, `derived_from` |
| `entities.csv`: `species, ncbi_taxon_id, species_role` | `species_context[]` |
| `entities.csv`: `assertion_type` (asserted/inferred) | `claim_category` (`paper_asserted` / `system_inferred`) |
| `evidence_sentence, evidence_section, evidence_chunk_id, evidence_char_start, evidence_char_end` | `source_excerpt`, `source_location[].{section, chunk_id, char_start, char_end}` |
| `evidence_verified, evidence_similarity, extraction_flag` | `evidence_verification`, `evidence_similarity`, `extraction_flags` |
| `field_provenance` (`field=label;label\|…`) | `field_provenance[]` (`field_name`, `provenance_labels`; replace `:` with `_` in each label) |
| `mappings.csv`: `mapping_id, ait_node_id/cl_id, ait_cell_type_label, skos_relation, match_confidence, basis_for_match, mapping_evidence, no_match_reason, nearest_ancestor_node_id, supersedes_mapping_id, mapped_by` | `ConceptMapping.{mapping_id, mapped_curie, mapped_label, skos_relation, match_confidence, basis_for_match, mapping_evidence, no_match_reason, nearest_ancestor, supersedes_mapping, mapper_type}`; `skos_relation: none` → `mapping_status: no_match` |
| `ait_taxonomy_used`, `reference_taxonomy` | `ConceptMapping.ontology` / `ExtractionRun.ontology_versions_used` → `OntologyVersion` |

These columns are specific to cell-type taxonomy mapping and stay out of the core. They belong in a cell-type extension schema that imports it: assay metadata from `methods.csv` (platform, modality, gene panel, read/UMI/gene medians, QC counts, donors, clustering method/resolution, annotation method), marker genes, `hierarchy_level`, the `entity_cards.csv` gene-set comparison (shared/paper-only/taxonomy-only genes, Jaccard, panel limits), `taxonomy_deposited`, and the derived `review_sheet.csv` view.

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

# Regenerate the JSON-LD context after changing the schema (adds "@version": 1.1)
python schemas/extraction_core/gen_jsonld_context.py
```

`check_dataset.py` adds the rules that JSON Schema cannot express:

- every reference resolves inside the dataset;
- `extracted_by` is one of the run's `models`, and a model reviewer is one of its review run's `models`;
- a record whose grounding is `not_found` cannot be `accepted`;
- an `accepted` concept mapping cannot be `model_proposed_unverified`;
- a concept mapping names a target unless it is `no_match`, and a `no_match` needs a `no_match_reason`;
- `match_confidence` follows from the SKOS relation (exactMatch → exact, other matches → partial, no_match → none);
- an accepted mapping has a `skos_relation`;
- an entity's SKOS slots hold exactly its accepted mappings.

## Files

- `extraction_core.yaml`: the schema.
- `check_dataset.py`: the validator, which also runs the cross-record provenance rules.
- `extraction_core.context.jsonld`: the JSON-LD context (generated; `"@version": 1.1`).
- `gen_jsonld_context.py`: regenerates the context, or with `--check` verifies it is current.
- `examples/ExtractionDataset-hu2026.yaml`: a complete valid example with mentions, an assertion, evidence, an evidential bearing, a relation, a resource, a judge review and a cluster.
- `examples/invalid/`: datasets that must fail. One is missing provenance; the other has broken references and policy violations.
- `.linkmllint.yaml`: the lint configuration used in CI.

Tests are in `tests/test_extraction_core.py`. They run in the CI job "Extraction Core schema" and are skipped locally when `linkml` is not installed.
