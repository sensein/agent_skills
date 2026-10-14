# Resource extraction → resource knowledge graph (BrainKB Resource Ontology)

## Goal

From a paper, preprint, README or model card, extract the **research resources** it
describes or uses — datasets, software, models, pipelines, workflows, archives,
schemas, ontologies, taxonomies, benchmarks, leaderboards, devices, material
collections, services, protocols, notebooks — and deliver them as a **resource KG**
in the BrainKB Resource Ontology (BKR).

The point is not a list of names. Per resource, the KG records what makes it safely
reusable, each statement tied to a verbatim quote and to the paper:

| concern | BKR | from the extraction |
|---|---|---|
| what it applies to — claimed / shown / used on / ruled out | `bkr:DeclaredScope` / `ValidatedScope` / `ObservedScope` / `OutOfScope` | `applicability.{declared,validated,observed,out_of_scope}` |
| on which taxa, anatomy, cell types, assays, conditions | `bkrls:appliesToTaxon`, `appliesToAnatomicalStructure`, `appliesToCellType`, `appliesToAssay`, `appliesToCondition`, `bkr:appliesToTask` / `Topic` / `Modality` | scope dimensions (concept labels, tool-mapped) |
| what it assumes | `bkr:Assumption` (+ kind subclasses): criticality, status, `violationConsequence`, `isTestableBy` | `assumptions[]` |
| how it fails | `bkr:FailureMode`: condition, symptom, mitigation, severity, `bkr:isSilentFailure` | `failure_modes[]` |
| what it needs / takes / gives | `bkr:Requirement`, `InputSpecification`, `OutputSpecification` | `requirements[]`, `inputs[]`, `outputs[]` |
| how well it did | `bkr:BenchmarkResult` (`dqv:QualityMeasurement`) | `benchmark_evidence[]` |
| identifiers, versions, access | `adms:Identifier`, `bkr:ResourceVersion` (+ change records), `bkr:AccessCondition`, licence | `stable_identifiers`, `versions`, `access`, `license` |
| which paper attests it | `dcterms:isReferencedBy` → the paper's `ner:Publication` | filled by the pipeline |
| the metadata record and its quotes | `dcat:CatalogRecord`, `bkr:ResourceAssertion` → `ner:EntityMention` (`ner:evidenceText`) | `provenance.field_evidence` |
| what was looked for and not found | `bkr:notFoundField` on the record | `not_found_fields` |

The four-way scope split is the reason for the ontology: a catalogue that collapses
"applies to" overstates its sources.

## Files

| file | role |
|---|---|
| `default_ontology/brainkb_resource_ontology.owl` | the ontology: the BKR standalone (core + vocabularies + life-science axes + BrainKB/SEPIO and schema.org/SOSA/DataCite bridges merged, no `owl:imports`). Also the vocabulary the converter reads `extracted_type → class` from (`bkr:instantiatesClass`). |
| `default_ontology/brainkb_resource_shapes.ttl` | the policy shapes (SHACL), deliberately outside the ontology |
| `default_ontology/resource_kg_config.json` | instance base, concept routing, grounding, source-silence shapes |
| `schemas/bkr-resource-extraction.schema.json` | the extraction contract (30 fields per record) |
| `prompts/extractor-resource.md` | the extractor prompt |
| `scripts/resource_kg.py` | records → ground → map → convert → stubs → link; and `validate` |
| `scripts/bkr_convert.py`, `scripts/bkr_stubs.py` | the BKR converter and mention-stub resolver (vendored from bkr-0.5.6) |
| `cqs/brainkb_resource_ontology_CQs.md` | 21 competency questions, runnable with `cqs/run_cqs.py --entail` |

## Pipeline

```
document ─▶ extraction JSON ─▶ ground ─▶ map ─▶ provenance ─▶ BKR Turtle ─▶ stubs ─▶ SHACL gate
          (extractor-resource.md,   (source   (concept_     (paper +      (bkr_convert)  (bkr_stubs)  (validate_ttl)
           bkr-resource-extraction   text)     mapping.json)  run)
           schema)
```

```bash
# framework mode
python -m scripts.pipeline --task resource --input paper.txt --mapper config
# or, with a result JSON in hand (host-model mode, or an earlier run)
python -m scripts.json_to_ttl result_final.json --source paper.pdf         # auto-detects resources
python -m scripts.resource_kg build result_final.json --source paper.pdf --map   # same, with concept mapping
python -m scripts.validate_ttl result.ttl                                    # gate: must exit 0
python cqs/run_cqs.py result.ttl --cqs cqs/brainkb_resource_ontology_CQs.md \
    --with-ontology default_ontology/brainkb_resource_ontology.owl --entail  # competency questions
```

### 1. Extract — two tiers

The extractor reads the whole document (`extraction_chunk_chars`, default 60 000;
records from several chunks are merged by name). It writes:

- a **deep record** for each resource the document describes (usually what it
  produces): description, identifiers, versions, scope, assumptions, failure modes,
  benchmarks, IO, owners, access, mentions;
- a **catalogue record** for each third-party resource it uses: name, type,
  identifier, version and an **observed** scope recording how this study used it;
- **mentions** for resources only named in passing.

Inclusion: resources produced, and third-party resources actually used. Exclusion:
wet-lab consumables (probes, kits, antibodies, stains), instruments used only as
equipment, figure/table/supplement labels, journal names, cited papers as such, and
bare method names with no named implementation. A key-resources table or a
"Data and code availability" section is authoritative for identifiers, RRIDs,
versions and URLs.

### 2. Ground — only what the source states

`resource_kg.ground` checks every record against the normalised source text (NFKC,
quotes and dashes folded, soft hyphens and line-break hyphenation removed — the same
normalisation the AIT evidence check uses):

| checked | rule | on failure |
|---|---|---|
| `stable_identifiers[].value` | stated as a complete token (DOI with or without `https://doi.org/`, RRID with or without `RRID:`) | removed; field added to `not_found_fields` |
| `versions[].version` | stated as a complete token (`1.4` is not `1.4.0`) | removed |
| `url`, `license` | stated in the text | removed |
| every evidence `quote` | found verbatim (fuzzy ≥ 0.95 tolerated for extraction noise) | quote dropped |
| `start` / `end` | never trusted (model-computed offsets) | removed; the quote is the anchor |
| a `ValidatedScope` | must keep evidence or a benchmark result (BKR invariant 2) | demoted to a declared scope |
| an `llm_judgment` mapping | never carries an IRI | IRI dropped, label kept |
| `mentions[].name` | named in the text | removed |

Removals are listed in the result (`resource_grounding.removed`) and the build
report. Without a source text nothing can be checked, so identifiers, versions,
URLs, licences and quotes are all removed — pass `--source`.

### 3. Map concepts — tools only

Scope-dimension labels go through the same `ConceptMapper` cascade as NER
(`concept_mapping.json`: trusted ontologies by `priority.md`, then local hybrid,
then BioPortal). `resource_kg_config.json` `concept_routing` says which extractor
label restricts each dimension's ontologies:

| dimension | routed as | ontologies (label_routing) |
|---|---|---|
| species | Species | NCBITaxon |
| anatomical_structures | BrainRegion | UBERON |
| cell_types | CellType | CL, PCL |
| developmental_stages | DevelopmentalStage | UBERON, MmusDv, HsapDv |
| assays, modalities | Method | OBI, EFO |
| conditions | Disease | MONDO, DOID |
| tasks, topics, variables | — not mapped | the label survives, unmapped |

Each hit becomes a `ner:ConceptMappingDecision` with the method (`normalized_lexical`
for trusted files, `hybrid_retrieval` for the local service, `exact_lexical` for a
BioPortal label hit, `rule_based` for curated mappings), relation (from the match
tier) and status. Exact/close tiers are `accepted` and become SKOS shortcuts on a
per-batch label concept; weaker tiers stay `proposed` — recorded, not asserted on the
scope. A tie between two classes is `ambiguous`, with the candidates named.

### 4. Provenance and identity

- The paper is the **same node** the NER graph of that paper uses: its
  `ner:Publication` IRI is derived exactly as `json_to_ttl` derives it (DOI, else
  source id/path), so the resource KG and the NER KG join on it. Every resource is
  `dcterms:isReferencedBy` it; every record `prov:hadPrimarySource` it.
- The run is a `bkr:AutomatedExtraction` associated with the structsense
  `ner:PipelineAgent` and the extractor model.
- A **resource is one node across papers** (UUIDv5 of its normalised name), so
  "which papers use Scanpy" is one hop. Its records, scopes, assumptions and quotes
  are per paper (keyed on a hash of the paper id and the record id), so two papers'
  claims never merge. Set `global_resource_key: false` for one node per record.
- Because a resource is shared, **every claim says which paper made it**:
  `bkr:scopeAssertedIn` on each scope, `bkr:assumptionStatedIn` on each assumption,
  `prov:hadPrimarySource` on benchmark results, failure modes, limitations,
  inputs/outputs, versions and every evidence quote; a paper's mentions are
  `dcterms:references` from its own record. Two papers' readings of one resource can
  be compared claim by claim.
- Instance IRIs are UUIDv5 under `https://brainkb.org/kb/` (the NER base). Output is
  byte-identical across runs; each node keeps its derivation key as
  `dcterms:identifier`. Changing `instance_base` re-keys every instance.

### 5. Mention stubs

Each `mentions` entry becomes a stub (`dcterms:identifier "mentioned/<name>"`).
`bkr_stubs.resolve` merges a stub onto the full record of the same resource
(normalised name, parenthetical / prefix / suffix variants, identifier values, URLs).
A stub with no full record is kept: a resource the paper names but does not describe
is a fact. Pass `--alias aliases.json` (`{mention: resource name}`) for variants the
rules miss; run `python -m scripts.bkr_stubs corpus.ttl --out corpus.ttl` over a
merged corpus to join a stub in one paper onto another paper's record.

### 6. Validate

`python -m scripts.validate_ttl x.ttl` recognises a resource KG and gates it with
`resource_kg.validate_graph`:

- SHACL against `brainkb_resource_shapes.ttl` with the ontology;
- every `bkr:`/`ner:` class and predicate declared in the BKR or NER ontology;
- one connected component.

**Source silence is a finding, not a defect.** A paper rarely states a licence or an
access condition, so `CitableResourceShape` ("a citable resource must state a
licence, a rights statement or an access condition") fires on most records. Those
results are reported as findings and do not fail the gate. `ScopedResourceShape` (a
tool/model must declare a scope) is a finding only when the gap is declared — the
record lists `applicability` in `not_found_fields`, or the node is a mention stub —
otherwise it is an extraction gap and fails. **Never fix a finding by inventing a
licence or a scope.**

### 7. Query

`cqs/brainkb_resource_ontology_CQs.md` holds the 21 BKR competency questions
(resources for a species, declared vs validated, applied outside declared scope,
critical / violated assumptions, deprecated versions and replacements, breaking
changes, mapping audit, failure modes, attesting works, extraction completeness, …).
`bkr:hasScope` and `bkr:appliedToConcept` are **entailed, never asserted** — run with
`--entail` (OWL-RL over data + ontology; needs `owlrl`) or most queries return
nothing.

## Legacy input

The earlier structsense resource shape (`schemas/resource-output.schema.json`:
`{"extracted_resources": {"1": [{name, type, category, target, specific_target,
url, identifiers, versions, mentions{datasets, tools, ...}}]}}`) is still accepted
and upgraded through the crosswalk the ontology itself declares
(`bkr:structsenseField`): `type` → `extracted_type` (Paper → publication),
`category` → `tasks`, `target`/`specific_target` → a declared scope's target labels,
NCBITaxon `mapped_specific_target_concept` → `species`, `identifiers`/`versions` →
`stable_identifiers`/`versions` with their quotes as field evidence, `mentions{}` →
typed `mentions[]`, `judge_score`/`remarks` → `judge`. A legacy record with no target
records `applicability` as not found. The output is a BKR resource KG either way.

## Judging

The judge scores records, not mentions: are `name`, `extracted_type`, identifiers
and URL consistent with the quotes; is the scope split honest (a validated scope
with a reported evaluation, not a claim); are assumptions and failure modes stated
in the text. A score is carried as `judge` → `dqv:QualityMeasurement` on the record.
