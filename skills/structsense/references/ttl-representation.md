# TTL representation — the deliverable

**The result of a NER or resource run is Turtle: one `<stem>.ttl` per paper**,
instances of the default ontology (the Named Entity Ontology,
`default_ontology/named_entity_ontology.owl`, v2.5.0, namespace
`https://brainkb.org/ner/`), gated by its SHACL shapes. Entity-focused `.entities.json` and `.entities.ttl`
are companion views (see `entity-identity.md`). Raw working JSON is internal:
the stages still hand each other JSON (extract → align → judge), but that is
working state under `<out>/.structsense/`, kept only with `--keep-json`.

**Identity is built in.** Every IRI is a deterministic UUIDv5 (below), and the
ones that must agree across papers are minted from what makes them the same
thing: an entity from its `ner:normalizedEntityKey`, a concept from its IRI, an
ontology hub from its acronym. Two papers that both mention the olfactory bulb
produce the same `kb:f4fbe5ee-…` node, so their graphs merge by plain union — no
rewrite step. Loading into a store (named graphs, access) is the `brainkb` skill's
job; what it loads is already identity-consistent. That makes the key the most
important string in the file (references/key-normalization.md).

## Who writes what

| part | written by | how |
|---|---|---|
| entities, mentions, offsets, sentences, sections, source models | `scripts/json_to_ttl.py` | deterministic, from the judged working JSON |
| class of each entity | `default_ontology/label_class_map.json` | label → most specific declared class; kg_plan may refine |
| ontology concepts + skos tier | `json_to_ttl.py` | ONLY `concept_mapping_provenance: "tool"` (trusted ontologies first — `concept_mapping.py`); tier from kg_plan, then the mapping judge (pass → exactMatch), then how the tool matched (`match_type_tiers`), then closeMatch |
| relations, hierarchy, causal claims | the **extractor** (per-mention `relations` / `broader`, cns-cells `cell_context`, document `causal_relations`) and kg_plan | resolved to entities by `scripts/relations.py`, reviewed by the claims judge, written by `json_to_ttl.py` |
| normalized key (no kg_plan entry) | `json_to_ttl.py` | a reviewed identity key, source-defined abbreviation or context-reviewed exact mapping; otherwise the algorithm (entity-identity.md) |
| judge verdicts | `json_to_ttl.py` | every review → `ner:ReviewDecision` attributed to its judge |
| normalized keys, finer classes, relations, causal claims | **you**, in `kg_plan.json` (prompts/kg-plan.md) | reviewed by the kg-keys and claims judges before conversion |

Use the converter to write Turtle rather than constructing it manually;
a converter gets every one of them structurally right, and the model's judgment
is spent where a script cannot decide.

## Workflow (host-model mode)

```bash
W=out/.structsense                            # working state, not the deliverable
# extract as usual → $W/<stem>_final.json, then map (trusted ontologies by priority → local → BioPortal):
python -m scripts.concept_mapping map $W/<stem>_final.json
python -m scripts.normalize_result $W/<stem>_final.json --llm-model <your model id>
# write $W/kg_plan.json per prompts/kg-plan.md           (default step; {} if nothing to add)
python -m scripts.judge_prepare $W/<stem>_final.json --source $W/<stem>.txt --kg-plan $W/kg_plan.json --out-dir $W/judge
# ... one judge at a time → $W/judge/reviews/*.json  (references/judge-ensemble.md)
python -m scripts.judge_combine $W/<stem>_final.json --reviews $W/judge/reviews/*.json --kg-plan $W/kg_plan.json
python -m scripts.json_to_ttl   $W/<stem>_final.json --kg-plan $W/kg_plan.json --source paper.pdf --out out/<stem>.ttl
python -m scripts.validate_ttl  out/<stem>.ttl                # must exit 0 before handoff
```

Framework mode: `python -m scripts.pipeline --input paper.txt --extractor … --judge …`
does all of it and writes `<stem>.ttl` (`--keep-json` also keeps the working JSON).

## IRIs and namespaces

```turtle
@prefix ner:  <https://brainkb.org/ner/> .       # target ontology
@prefix kb:   <https://brainkb.org/kb/> .        # every instance, as kb:<uuid>
@prefix obo:  <http://purl.obolibrary.org/obo/> .
@prefix BKE:  <https://identifiers.org/brain-bican/vocab/> .   # non-OBO prefixes actually used
```

Instances are `kb:<uuid5(uuid5(NAMESPACE_URL, "https://brainkb.org/kb/"), name)>`
(`default_ontology/ttl_config.json` → `iri`):

| node | name | scope |
|---|---|---|
| entity | `entity|<normalizedEntityKey>` | shared across papers |
| ontology concept | `concept|<conceptIRI>` | shared across papers |
| ontology hub | `ontology|<ACRONYM>` | shared across papers |
| agent / software | `agent|<id>` | shared across papers |
| publication, document version, sentences, sections (`paper_shared_kinds`) | `<kind>|<DOI>|<local id>` | this paper, every extraction of it |
| everything else (mentions, annotation versions, classifications, reviews, decisions, runs, causal) | `<kind>|<DOI>|<variant>|<local id>` | this paper AND this extraction |

`<variant>` is `task_type:ner_domain` (`ner:neuroscience`, `ner:cns-cells`; override
with `json_to_ttl --variant`). Without it, the neuroscience and cns-cells TTLs of the
same paper minted the same IRI for "mention 3 of neuropixel": a store holding both
showed one classification node with two labels, two confidences and two raw labels.
Entities and concepts stay shared, which is the point: both readings are about the
same entity.

Deterministic: re-running a paper reproduces the same IRIs. `iri.scheme: "slug"`
gives readable `kb/<paper-slug>/<kind>-<slug>` IRIs for debugging only; the gate
rejects them unless that scheme is set, and rejects **blank nodes always** — an
unnamed node cannot be merged, referenced or reviewed.

**The document.** `ner:Publication` carries title, DOI, PMID/PMCID, `dcterms:issued`,
journal (`dcterms:bibliographicCitation`) and its authors — `prov:wasAttributedTo`
and `dcterms:creator` to `prov:Person` nodes (UUID from `agent|orcid:<id>` or
`agent|person:<name>`, shared across papers), with the printed order in an
`rdfs:comment` and where each field was read from (`scripts/doc_metadata.py`). A PDF
or XML source gives two `ner:DocumentVersion`s: the source document and the extracted
text, `prov:wasDerivedFrom` it and `prov:wasGeneratedBy` a
`ner:DocumentIngestionActivity` associated with the loader backend (a software agent
with its version). Mentions and offsets belong to the text. `ner:sourcePath` is the
file name only.

**Labels are names.** `rdfs:label` is what a graph viewer draws on the node, so it
is short (≤ `labels.max_length`, 60) and never prose: entities carry their
normalized label, mentions their surface form, structural nodes their kind
("annotation v1", "classification CellType", "grounding review", "mapping decision
CL:0000617", "sentence 12", "judge ensemble report"). What a node relates is in its
properties; explanations are strings — `rdfs:comment`, `ner:reviewComment`,
`ner:sentenceText`. A kg_plan `normalized_label` with a parenthetical gloss
("L-phenylalanine (Phe; amino acid odorant; CS in some groups)") is split: the name
keeps the abbreviation, the gloss becomes a comment; a plan `note` is a comment too.

**Prefixes are one registry** (`scripts/prefixes.py`): OBO prefixes and
`curie_expansions` from ttl_config.json, plus each trusted ontology's own
namespace under its priority.md CURIE prefix. The CURIE of every
`ner:OntologyConcept`, the acronym of every `ner:ExternalOntology` and the
`@prefix` lines all come from it, and the validator checks that each
`conceptIdentifier` uses the canonical prefix and expands to its `conceptIRI`. An
IRI in no registered namespace is not written as a concept (it is reported, with
the fix: register the namespace) — a guessed prefix is how one namespace ends up
under two names.

## What the file contains (profile `full`, the default)

**Spine** — `ner:Publication` (doi, title) ⇄ `ner:DocumentVersion` (mediaType,
sha256 `checksum`, sourcePath); `ner:NERExtractionActivity` (taskType,
runIdentifier, `prov:used` docv, `prov:wasAssociatedWith` every source model,
`ner:usedSoftware` structsense); `ner:ExtractionSnapshot` (`prov:hadMember`
every entity); a `ner:ConceptMappingActivity` (the mapper, `usedOntologyVersion`)
and an `ner:AutomaticValidationActivity` (the judges).

**Per entity** (every canonical entity, key term and resource):

```turtle
ex:entity-sst-interneuron a ner:Interneuron, ner:NamedEntity ;  # most specific class + NamedEntity
    ner:normalizedEntityKey  "sst_interneuron" ;                 # REQUIRED — ingestion merges on it
    ner:normalizedEntityLabel "somatostatin-expressing interneuron (SST-IN)" ;
    rdfs:label "somatostatin-expressing interneuron (SST-IN)" ;
    ner:hasMention ex:m-sst-interneuron-1, ex:m-sst-interneuron-2 ;   # one per raw occurrence
    ner:resolvedToConcept ex:c-CL_4023017 ;                      # iff tool-verified
    skos:exactMatch obo:CL_4023017 ;                             # tier = honest identity strength
    obo:RO_0001025 ex:entity-neocortex ;                         # only paper-stated (kg_plan, claims-judged)
    ner:hasReviewDecision ex:review-sst-interneuron-mapping ;    # one per judge verdict
    prov:hadPrimarySource ex:publication .
```

**Extraction-event record (v2.4)** — who, what, when, with which settings:

```turtle
kb:<run> a ner:NERExtractionActivity ;
    ner:runIdentifier "structsense-…" ;
    prov:startedAtTime "…Z"^^xsd:dateTime ; prov:endedAtTime "…Z"^^xsd:dateTime ;  # when recorded
    prov:used kb:<docv> , kb:<config> ;                    # what it read, with which settings
    prov:wasAssociatedWith kb:<llm-agent> , kb:<structsense> .
kb:<llm-agent> a ner:LanguageModelAgent ; ner:agentVersion "<model id>" .
kb:<config> a ner:ConfigurationArtifact ; ner:configurationHash "sha256:…" ; ner:schemaVersion "2.4.0" .
kb:<snapshot> a ner:ExtractionSnapshot ; prov:wasGeneratedBy kb:<run> ; prov:generatedAtTime "…Z"^^xsd:dateTime .
```

Times are **recorded, never inferred**: `pipeline.py` stamps the run
(`run_metadata`), `concept_mapping map` stamps the mapping step, `judge_combine`
stamps the ensemble. In host-model mode pass `json_to_ttl --started-at/--ended-at`
if you timed the run; without them the run carries no times (the snapshot always
records when it was generated). `configurationHash` is sha256 over every config
file in effect (concept_mapping.json, priority.md, judges_config.json,
ttl_config.json, label map, key overrides, ontology, shapes), so two runs with
different settings are distinguishable. `ner:publicationDate` is written only for
a full date the source states; a bare year goes to `dcterms:issued` (`xsd:gYear`).

**Per mention** — `ner:EntityMention`: `surfaceForm` (verbatim),
`documentStartOffset` / `documentEndOffset`, `partOfDocumentVersion`,
`inSentence` (a shared `ner:Sentence` with `sentenceText`), `inSection` (from
`paper_location`), and an `ner:EntityAnnotationVersion` → `ner:EntityClassification`
(raw label, class, the HF model's confidence, cell `specificity`), attributed to
the model that surfaced it (rule 11).

**Per tool-verified concept** — `ner:OntologyConcept` (`conceptIRI`
xsd:anyURI, `conceptIdentifier` CURIE, `preferredLabel`) →
`ner:OntologyVersion` → `ner:ExternalOntology` hub, plus a
`ner:ConceptMappingDecision` (relation type, status accepted, provenance "tool",
alignment method, `decisionConfidence` = the mapping judge's confidence) selecting a
`ner:ConceptMappingCandidate` (`mappingRank` 1). Each mention's annotation version
points at them (`hasMappingDecision` / `hasMappingCandidate`), and each decision
`prov:wasGeneratedBy` the **per-source** `ner:ConceptMappingActivity` that made it —
trusted ontologies, local hybrid, BioPortal — each with its own
`ner:usedMappingMethod`, under one timed mapping step.

**Judging as provenance** — every panel member is its own
`ner:AutomaticValidationActivity` (versioned agent, mode, and the exact
`ner:PromptArtifact` it followed, by `promptHash`); each verdict is a
`ner:ReviewDecision` generated by that activity (`ner:reviewDimension` = the judge,
`ner:reviewConfidence`), attached to the entity and to every mention's current
annotation version, plus one "ensemble-combined" decision per item whose confidence
is the aggregated judge_score and which `prov:wasDerivedFrom` each judge's; the deterministic combine step and
the LLM combiner are activities of their own; and **every change the ensemble made**
is a `ner:ChangeRecord` — `ClassificationChangedChange` (relabel),
`MappingChangedChange` (tier), `CanonicalFormChangedChange` (key rename),
`MappingRemovedChange` (demoted IRI), `MentionRemovedChange` (dropped
hallucination: surface, offsets, reason), `CausalRelationRemovedChange` — with
before/after values, the reason, `triggeredByActivity`, and `hasReviewDecision` to
the review that licensed it. The snapshot carries a `ner:ValidationReport`. The
annotation's `classificationConfidence` is the ensemble `judge_score`.

Profile `compact` (`--profile compact`) keeps entities, mentions (surface,
offsets, docv), concepts and kg_plan edges, and drops the annotation / decision /
review layer — for very large papers where triple count matters more than audit.

## Non-negotiables

1. **Tool-verified IRIs only.** No `concept_mapping_provenance: "tool"` → no
   external IRI; the entity stays internal with an `rdfs:comment` naming the gap,
   the mapper and the date. Never map from model knowledge — not even a "coarser
   verified term" you are sure of: every `skos:*Match` must be the `conceptIRI` of
   one of the entity's own `ner:resolvedToConcept` nodes (SHACL
   `MatchBackedByConceptShape`).
2. **The skos tier states identity strength, never connectivity ambition.**
   exactMatch = is that concept; closeMatch = near-identity, caveat in a comment;
   broadMatch / narrowMatch = super / subclass; relatedMatch = thematic.
   Homology is not identity: zebrafish pDp gets `rdfs:seeAlso` piriform cortex.
   One tier per IRI per entity.
3. **One connected component per file.** Mentions → docv, entities →
   publication, concepts → ontology hubs, reviews → judge agents, and every
   activity → the run. An island is an entity or claim that lost its provenance.
4. **rdfs:label on every per-paper node.**
5. **Class discipline** — the most specific *declared* class the paper's wording
   licenses, plus `ner:NamedEntity`. A label with no class
   (`label_class_map.json` has no entry, no class of that name) is typed with the
   fallback class and says so in a comment; the raw label is always kept in
   `ner:classificationLabelRaw`.
6. **Causal claims are this paper's.** `ner:CausalRelation` (hasCause /
   hasEffect / hasMediator / hasModerator / hasConfounder) with a
   `ner:CausalRelationVersion` (revision 1, `causalNegated`,
   `causalHypothetical`, type / polarity / modality / directness /
   evidence basis from the ontology's controlled vocabularies, evidence in
   `rdfs:comment`), an optional `ner:EffectEstimate`, ordered in a
   `ner:CausalChain` via `nextCausalRelation`. `causalHypothetical false`
   requires an interventional evidence basis (SHACL).

**Relations between entities** — paper-stated RO/BFO edges (the closed list in
`ttl_config.json` `relation_predicates`: part_of, located_in, expresses,
has_phenotype (RO:0002200), in_taxon, capable_of, develops_from, …), the in-paper
hierarchy as `skos:broader` (CellSubtype → CellType → CellClass, region → larger
region), `skos:related`, `rdfs:seeAlso` (homology), `prov:used` /
`prov:wasDerivedFrom`. They come from the extraction itself for every NER domain
and from kg_plan; each passes the claims judge first, and its verdict hangs off the
subject entity as a `ReviewDecision`. SHACL requires both ends to be extracted
entities, no self-loops, and acyclic `skos:broader` and part-of; the validator
rejects any RO/BFO predicate not in the config and warns when a target falls
outside `relation_range_hints` (has_phenotype → a phenotype-like class, expresses →
a molecular entity, …).

## The gate — `scripts/validate_ttl.py`

| layer | checks |
|---|---|
| OWL vocabulary | declared classes, properties and controlled terms only; domain/range under subclass closure; no untyped per-paper node as an object; one concept node per identifier |
| SHACL (`default_ontology/named_entity_shapes.ttl`, with the ontology + RDFS inference) | entity: ≥1 mention, exactly one key matching the key grammar and not on the guardrail list, label, primary source; every match IRI backed by a resolved concept and every concept tiered; mention: surface, docv, offsets end > start and length = surface length, not orphaned; concept: IRI (anyURI), CURIE, ontology version; mapping decision provenance = "tool"; review: status, attributed judge; causal: cause ≠ effect, version back-link, booleans, interventional evidence for non-hypothetical; effect estimate: p in [0,1], CI lower ≤ upper |
| identity & labels (policy) | every instance `<iri.base><uuid>`; no blank nodes; no `rdfs:label` over `labels.max_length` |
| graph shape | rdfs:label on every per-paper node; one connected component |
| `--check-ols` (optional) | every conceptIRI resolves in OLS4 — existence only; meaning is the mapping judge's |

Warnings (unmapped entity with no gap comment) are reported and do not fail the
gate. A mention whose sentence does not contain its surface (a PDF sentence cut at
a hyphenated line break) is repaired by the converter from the source text at the
mention's offsets when `--source` is given. **0 violations before handoff.**
Validate one paper per invocation (concept and entity nodes are shared by UUID,
so a merged multi-paper file is also valid, but per-paper reports are clearer).

`examples/ttl/hu2026.ttl` is a full, validating result for a real open-access
paper (Hu et al. 2026, CC BY 4.0), produced by `examples/ttl/run.sh`; its entity
and concept IRIs are identical to the human-curated graph's. Diff against it when
debugging.

## A different target ontology

Pass `--ontology` (and a label map / shapes of your own) to both scripts, and
`--ns` to the validator. The structural pattern — entity / mention / document /
provenance, tiers, gaps, labels — transfers as long as the ontology has the
corresponding classes and properties; the validator tells you immediately
where it does not.

The default profile is compact. Use `--profile full` for audit records.
SourceDocument covers arbitrary unstructured sources; Publication specializes it.
RelationAssertion records retain each occurrence’s evidence and qualifiers.
