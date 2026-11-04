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
| `scripts/resource_activity.py` | network, on by default: is each resource still maintained? (GitHub/GitLab, PyPI, CRAN, Bioconductor, npm, conda; ontologies via OBO Foundry + OLS; RRIDs; GEO datasets) |
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
python -m scripts.resource_kg build result_final.json --source paper.pdf --no-check-activity  # offline: skip the activity check
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
| a URL / identifier the PDF wraps across lines | also matched with all whitespace removed and a hyphen at the break optional (values ≥ 12 characters); the source's spelling is adopted, so `…single-` ⏎ `cell-atlas` keeps its hyphen | kept; listed under `rejoined` |
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
  `ner:Publication` IRI is derived exactly as `json_to_ttl` derives it (source id,
  else DOI, else `preprint:<id>` for an unregistered bioRxiv/medRxiv preprint, else
  the source path), so the resource KG and the NER KG join on it. Every resource is
  `dcterms:isReferencedBy` it; every record `prov:hadPrimarySource` it.
- **The paper, not the text file.** Its identity and bibliographic metadata come from
  the document first — JATS front matter, the DOI and licence printed in the text
  (bioRxiv headers: `doi.org/10.64898/…`, "made available under a CC-BY-NC-ND 4.0
  International license"), PDF metadata, the file name (a bioRxiv/medRxiv id becomes
  a DOI only once a registry confirms it, under `10.64898` or `10.1101`) — then from
  the registry the DOI resolves at (Crossref; bioRxiv's API) for title, authors (with
  ORCID), date, venue and licence (`metadata.resolve_doi`, on; `--no-resolve-doi` or
  `STRUCTSENSE_NO_NETWORK=1` keeps it offline). The extractor's `paper` block (rule
  13: what the paper prints) fills gaps, and every value is checked against the
  text. The publication carries `ner:title`, `ner:doi`, `ner:publicationDate`,
  `dcterms:issued`, `dcterms:bibliographicCitation` (venue), `dcterms:license`,
  `dcterms:creator` / `prov:wasAttributedTo` each author (`prov:Person`, the same IRI
  the NER graph mints), and an `rdfs:comment` saying where each field came from.
- **The text actually processed** is a `ner:DocumentVersion` of the paper (name,
  `text/plain`, sha256, length) — `prov:wasDerivedFrom` the source document (the PDF
  etc., also a `ner:DocumentVersion` with its own checksum) when there is one. A
  `<stem>_text.txt` is matched to `<stem>.pdf` beside it; otherwise pass both:
  `resource_kg build result.json --source paper.pdf --text paper_text.txt`. Text that
  existed only in memory is preserved as `<out>.source.txt`, so every quote can be
  checked against the exact text.
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

### 6. Activity — is the resource still maintained? (network, on by default)

A paper says where its code lives; it cannot say whether anyone still works on it.
Every resource conversion — `json_to_ttl`, `resource_kg build`, `pipeline --task
resource`, `batch` — asks the code hosts and package registries and records the
answer as a dated `bkr:ActivityCheck` on the resource — an observation, not a claim
of the paper, and separate from the paper-stated `bkr:lifecycleStatus` of a version.

Targets come from the graph: `bkr:accessURL`, URL identifiers, usage-example URLs,
and install commands in usage-example code (`pip install`, `install.packages`,
`BiocManager::install`, `conda install -c <channel>`, `npm install`). A GitHub Pages
site (`owner.github.io/repo`) points at `github.com/owner/repo` (inferred: a missing
repository there is not "unavailable"). A registry that names its source repository
(PyPI `project_urls`, CRAN/Bioconductor `URL`, npm `repository`, conda `dev_url`)
adds it as a target.

Beyond code: **ontologies** — an OBO PURL or term IRI, an OBO Foundry or OLS page, or
an exact OBO Foundry title (`Gene Ontology`) — get OBO Foundry's curated status
(active / inactive / orphaned, obsolete → deprecated), the release date of the
version OLS has loaded (not OLS's own load time), and their repository. **RRIDs** of
tools (`RRID:SCR_…`) are resolved for their links (often the repository or a
package) and a "no longer in service" mark; an RRID carries no dates itself.
**GEO series** (`GSE…`) are datasets: `available` or `embargoed`, with release and
last-update dates — a dataset is not stale for being unchanged. A **software
library with no link at all** is tried by exact-case name on Bioconductor, then CRAN
(first hit wins: limma lingers on CRAN as archived since it moved). Guesses from a
name are marked inferred and record nothing when they find nothing.

| status | rule (first that applies) |
|---|---|
| `archived` | the code host marks the repository archived |
| `deprecated` | a registry says so: npm `deprecated`, CRAN archived, PyPI `Development Status :: 7 - Inactive` |
| `inactive` (curated) | OBO Foundry lists the ontology inactive or orphaned |
| `unavailable` | every target answered 404/410 |
| `available` / `embargoed` | a dataset (GEO): released, or held back |
| `unknown` | no date could be read (unreachable, rate limited) |
| `active` | last commit / release / upload within `stale_after_days` (365) |
| `stale` | within `inactive_after_days` (1095) |
| `inactive` | older |

Dates that do not mean development are not used: conda uploads (channel rebuilds)
count only when nothing else dates the resource, and a Bioconductor release version
bump is not a commit. Thresholds are in `resource_kg_config.json` (`activity`) and
recorded on each check (`bkr:staleAfterDays`, `bkr:inactiveAfterDays`). Each source's
answer is a `bkr:ActivitySignal` (`bkr:lastCommitDate`, `bkr:lastReleaseDate`,
`bkr:latestVersion`, `bkr:isArchived`, `bkr:isDeprecated`, `bkr:isAvailable`,
`bkr:checkedURL`). It is on by default (`activity.enabled`); skip it with `--no-check-activity`,
`"enabled": false`, or `STRUCTSENSE_NO_NETWORK=1`, and add it later with
`python -m scripts.resource_activity annotate x.ttl`. Offline, it stops after 3
resources in a row get no answer and says so in the report, rather than waiting on
every timeout. The library calls (`resource_kg.build`, `json_to_ttl.result_to_ttl`)
stay offline unless passed `check_activity=True` (or `None`: follow the config).
Rate limited (GitHub allows 60 unauthenticated requests/hour, about 20 repositories):
nothing is recorded for that resource — a partial answer could hide an archived
repository, and an `unknown` would mask the last real check — the host is not asked
again in that run, and the report lists the resources to re-check. Set
`GITHUB_TOKEN` (or `GH_TOKEN`) for 5000/hour. Quick look without a graph:
`python -m scripts.resource_activity check https://github.com/org/repo "pip install x"`.

**Provenance — every assessment says when, by what, from where** (PROV-O,
structured after NIDM-Experiment; `skos:closeMatch`, not subclass, since NIDM's
acquisitions are about study participants):

```
ActivityCheckRun (cf. nidm:Session)      prov:startedAtTime / endedAtTime (xsd:dateTime, µs, UTC)
│                                        prov:wasAssociatedWith → prov:SoftwareAgent
│                                            nidm:NIDM_0000122 (software version, from SKILL.md)
│                                        bkr:staleAfterDays, bkr:inactiveAfterDays, bkr:checkCount
└─ ActivityCheck (cf. nidm:Acquisition)  dcterms:isPartOf run; prov:used the resource; start/end times
   ├─ ActivitySignal, one per source     prov:wasGeneratedBy check; prov:generatedAtTime (answer received);
   │                                     prov:hadPrimarySource each API URL queried
   └─ ActivityAssessment                 prov:wasGeneratedBy check; prov:generatedAtTime;
      (cf. nidm:AcquisitionObject)       prov:wasAttributedTo agent; prov:wasDerivedFrom signals;
                                         bkr:activityStatus, bkr:lastActivityDate, bkr:assessedResource;
                                         bkr:statusReason (the rule and evidence, as text);
                                         bkr:statusDecidedBy → the signal(s) that decided it
resource ── bkr:hasActivityAssessment ──▶ assessment;  resource ── bkr:hasActivityCheck ──▶ check
```

Runs accumulate and are never overwritten; the current status is the latest
assessment by `prov:generatedAtTime` (times have one fixed width, so they also order
as text). SHACL (`ActivityAssessmentShape`, `ActivityCheckShape`,
`ActivityCheckRunShape`) makes a missing generating check, time, status, resource or
agent a violation. Checks recorded before this (status on the activity, a date
only) are upgraded in place to assessments the next time `annotate` runs.

```sparql
PREFIX bkr: <https://brainkb.org/resource/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?name ?status ?last ?assessedAt WHERE {      # latest assessment per resource, not active
  ?r bkr:resourceName ?name ; bkr:hasActivityAssessment ?a .
  ?a prov:generatedAtTime ?assessedAt ; bkr:activityStatus ?status .
  OPTIONAL { ?a bkr:lastActivityDate ?last }
  FILTER NOT EXISTS { ?r bkr:hasActivityAssessment ?a2 . ?a2 prov:generatedAtTime ?t2 .
                      FILTER(STR(?t2) > STR(?assessedAt)) }
  FILTER(?status != <https://brainkb.org/resource/activity-status/active>)
} ORDER BY ?last
```

### 7. Validate

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

### 8. Query

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
