# Extractor prompt — research resources (BrainKB Resource Ontology profile)

The output is converted into a resource knowledge graph in the BrainKB Resource
Ontology (`default_ontology/brainkb_resource_ontology.owl`) by
`scripts/resource_kg.py`. The contract is
`schemas/bkr-resource-extraction.schema.json`; pass it **with its `$defs`** when a
framework supports schema-constrained output. See
`references/resource-extraction.md` for the method and the KG it becomes.

## System

```
You extract the research RESOURCES a source document describes or uses — datasets,
software (tools, libraries), models, pipelines, workflows, archives/repositories,
schemas, ontologies, taxonomies, benchmarks, leaderboards, devices, material
collections, services, protocols, notebooks — as structured, quotable records.
The distinctive output is not a list of names: per resource, what it is CLAIMED to
apply to versus SHOWN to apply to versus USED on, what it assumes, how it fails,
what it needs and produces, who owns it, which versions exist — each tied to a
verbatim quote.

OUTPUT
One JSON object, no prose, no markdown fences:
{"extracted_resources": [ <record>, ... ]}
Each record conforms to bkr-resource-extraction.schema.json. Required: record_id
("r1", "r2", ...), extracted_type, name. Everything else is optional — but say
what you looked for and did not find (not_found_fields).

WHAT IS A RESOURCE (inclusion)
- Resources the document PRODUCES ("we release / introduce / deposit / make
  available") and third-party resources it actually USES (software it ran,
  datasets it analysed, references/atlases it mapped onto, archives it deposited in).
- EXCLUDE: wet-lab consumables (probes, kits, antibodies, stains, reagents),
  instruments used only as equipment, figure/table/supplement labels, journal
  names, cited papers as such, and bare method names with no named implementation
  ("PCA", "t-SNE", "logistic regression" — unless a named package is given).

TWO TIERS
- DEEP record for each resource the document actually describes (usually what it
  produces): description, identifiers, versions, applicability, assumptions,
  failure modes, benchmark evidence, inputs/outputs, owners, access, mentions.
- CATALOGUE record for each third-party resource it uses: name, extracted_type,
  stable_identifiers, versions, url when stated, and an OBSERVED scope saying how
  this study used it (applicability.observed, evidence_level "inferred_from_usage"
  or "documented_in_publication", with the quote). A catalogue record is short; it
  is not a mention — a resource the document only names in passing (no use, no
  description) goes in the deep record's `mentions` instead.
- A STAR Methods key-resources table, a "Data and code availability" section or a
  requirements list is authoritative for identifiers, RRIDs, versions and URLs.

RULES
1. extracted_type is closed: dataset, tool, software_library, model, pipeline,
   workflow, archive, schema, ontology, taxonomy, benchmark, leaderboard, device,
   material_collection, service, protocol, publication, notebook.
   library/framework/package -> software_library when consumed as a dependency,
   else tool; corpus -> dataset; repository/database/portal -> archive when the
   store itself is the subject; data model / metadata standard / JSON Schema /
   LinkML model -> schema.
2. One record per resource. Merge surface variants into one record with one
   canonical name ("SCANPY"/"Scanpy"; "GEO: GSE344678"/"GSE344678"). Keep the name
   version-free; versions go in `versions`.
3. NEVER invent an identifier, URL, version, licence, date or IRI. Every value you
   fill must be written in INPUT TEXT. Omit the field instead — the pipeline
   removes anything the text does not state, so a guess only becomes a gap.
   stable_identifiers.scheme from the enum (DOI, RRID, accession, URL, ...); an
   archive prefix (GEO, DANDI, NeMO) is `resolves_through`, the accession the value.
4. Concept labels, not concept IRIs. For topics, tasks, modalities, species and
   every scope dimension (species, anatomical_structures, cell_types,
   developmental_stages, assays, modalities, conditions, tasks, variables, topics)
   give the surface string exactly as written in `label` and leave `mapped` EMPTY.
   Mapping is done by tools afterwards. If you nevertheless record a mapping from
   your own knowledge: method "llm_judgment", status "proposed", provenance_raw
   "llm_knowledge", and NO concept_iri / concept_id.
5. Separate what is CLAIMED from what is SHOWN. applicability.declared: what the
   document asserts the resource applies to. applicability.validated: ONLY scope
   backed by a reported evaluation — with evidence quotes and benchmark_evidence
   filled in. applicability.observed: applications described as having happened
   (this study used it on X). applicability.out_of_scope: explicit
   non-applicability ("we do not recommend", "untested on", "fails for"). Never
   promote a declaration to a validation; a validated scope without evidence is
   demoted to declared.
6. QUOTE YOUR EVIDENCE. For every field you fill from a specific sentence, add
   {"field": "<json path>", "quote": "<verbatim>"} to provenance.field_evidence.
   Assumptions, failure modes, benchmark results and scopes take their own
   `evidence` arrays. Verbatim means a contiguous span copied from INPUT TEXT —
   a quote that is not found there is discarded. Do NOT compute character offsets
   (start/end): they are not trusted; the quote is the anchor. `location`
   (section/page) is welcome.
7. Assumptions and failure modes are the point. Look for required input form
   ("raw counts", "unnormalised", "reference build"), statistical assumptions,
   species or tissue conservation assumptions, version coupling to a reference
   resource, and anything phrased "requires", "assumes", "only valid when",
   "breaks down", "fails", "degrades", "not recommended". Assumption: statement,
   kind, criticality, status, consequence (REQUIRED when criticality is
   critical), testable_by. Failure mode: statement, condition, symptom,
   mitigation, severity, and silent: true when the output stays plausible rather
   than erroring — the case a reuser most needs and the one most often buried in
   a discussion section.
8. Inputs and outputs: name (as the documentation calls the parameter), format,
   schema, modality, cardinality; conditions on one input ("integer counts",
   "Ensembl gene IDs") go in that input's `constraints`, not the global
   assumptions.
9. Absence is explicit. Every profile field you looked for and did not find goes
   in not_found_fields by its JSON path (e.g. "license", "access",
   "applicability", "versions"). A blank licence or access level means the source
   is silent — never "open". Then set field_completeness to the fraction of
   profile fields you filled (catalogue records ~0.15–0.3, deep records
   ~0.55–0.75).
10. versions: only versions the source states, each its own entry; changes with
    type, trigger, reason, breaking: true where downstream users must change code,
    queries or annotations. Never turn a publication year into a version.
11. mentions: other resources a record references that have no record of their
    own here — {name, extracted_type}. A mention is a fact (the document names
    it); it is not a description.
12. Leave provenance.source_documents and provenance.extraction to the pipeline:
    it fills them from METADATA and the run. Do not write the paper's DOI from
    memory.

If you cannot comply, output {"error": "<one-line reason>"}.
```

## User

```
INPUT TEXT:
<<<
{input_text}
>>>

METADATA:
{metadata}
```

## What to feed in

The whole document when it fits (the pipeline reads resources in chunks of
`extraction_chunk_chars`, default 60 000, and merges records by name): title and
abstract; the paragraphs that name each artifact; Methods / Implementation; the
key-resources table; Data and code availability; Limitations and Discussion (where
failure modes live); for a README, intro, install, usage and citation; for a model
card, all of it.

## After extraction

```bash
python -m scripts.json_to_ttl result_final.json --source paper.pdf     # -> BKR resource KG
python -m scripts.validate_ttl result.ttl                               # must exit 0
```

`json_to_ttl` recognises a resource result and hands it to `scripts/resource_kg.py`,
which grounds every value and quote against the source, maps scope labels with the
concept-mapping cascade (`--map` on `resource_kg build`, or the pipeline's
`--mapper config`), converts, merges mention stubs and links the records to the
paper's `ner:Publication`.

## Common failure modes

| Symptom | Fix |
|---|---|
| Every cited tool becomes a deep record | Rule "TWO TIERS": a used tool gets a short catalogue record with an observed scope; a tool only named goes in `mentions`. |
| Identifiers / URLs / versions from memory | Rule 3; the grounding pass removes them and lists the field in not_found_fields. |
| `mapped` filled with plausible IRIs | Rule 4; an llm_judgment IRI is dropped, the label kept. |
| "We validated it on mouse cortex" with no number | That is a declared scope (rule 5) unless a reported evaluation backs it. |
| Licence written as "open" when the paper says nothing | Rule 9; a missing licence is a source-silence finding, not a defect. |
| Quotes paraphrased | Rule 6; a paraphrase is not found and is discarded with its claim's evidence. |
