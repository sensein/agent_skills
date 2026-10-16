# Extractor — cell types mapped to Allen Institute (AIT) taxonomies

You are extracting structured information from a neuroscience publication and linking
its cell types to Allen Institute cell type taxonomies (AIT).

## Data model: the paper is the node

Everything you produce hangs off one paper node. Each extracted entity belongs to that
paper and carries a link to zero or more taxonomy entries. Mappings are edges, not
properties — they must be reversible, so that a query starting from an AIT cell type
returns every paper that has been mapped to it, with the evidence that justified each
edge. Practically this means:

- Every row in every output table carries `paper_id`.
- Every entity gets a stable `mention_id` of the form `{paper_id}:M{nnn}`, assigned once
  and never reused. After Pass 2 deduplication one `mention_id` denotes one entity; the
  surface mentions it absorbed are listed in `merged_mention_ids`.
- Every mapping row carries both `mention_id` and `ait_node_id`, so the edge can be
  traversed in either direction. A mapping row with a blank `ait_node_id` is not an edge;
  it is a recorded failure to map, and must state why.
- Nothing is overwritten. If a later pass revises a mapping, add a row with a new
  `mapping_id` and set `supersedes_mapping_id`.

## Order of operations

Four passes, then the review loop every structsense mode runs
(`references/review-loop.md`). Do not merge Pass 1 with Pass 3 — extraction must not
be biased by what the taxonomy happens to contain.

1. **Index and extract (one pass over the paper).**
2. **Verify** — lexical evidence check, deduplication, provenance labelling.
3. **Map** to AIT taxonomies with SKOS match relations.
4. **Assemble entity cards**, including the marker-gene diff.
5. **Review loop** — concept map every entity through the trusted ontologies
   (`python -m scripts.review_loop map --mode ait out/` → `concept_mappings.csv`:
   trusted files first, then the local mapper, OLS MCP, BioPortal; **genes through the
   species' genome annotation**, see below); the judge panel
   (`python -m scripts.record_judge prepare|combine --mode ait out/`, prompt
   `prompts/judge-record.md`, one judge at a time); optional human feedback
   (`python -m scripts.human_feedback queue|apply --mode ait out/`). Corrections
   append superseding rows to `mappings.csv` (`mapped_by` llm for a judge, human for a
   curator) and never overwrite; an entity a gate drops is quarantined
   (`unverified_evidence`). Re-run Pass 4 if an edge moved to another node, then
   `ait_tables validate`.
6. **Write the Turtle** — always: `python -m scripts.ait_ttl` turns the validated tables
   and `concept_mappings.csv` into `<paper_id>.ttl` and gates it. A run that stops at the
   CSVs is unfinished.

The flow is **extraction, then concept mapping against the trusted ontologies**. You
extract into the tables (Passes 1–4); the mapping of every entity to an ontology term is
done by code from the files in `trusted_ontologies/`, by entity type:

| Entity | Trusted source | Where it is recorded |
|---|---|---|
| cell type with an AIT edge | the taxonomy file (`Human_BG.ttl`, …) | your `mappings.csv` edge, node ids from `ait_nodes` |
| gene | first the species' genome annotation (`annotation-ENSEMBL-9606-109.ttl`, `annotation-NCBI-9544-103.ttl`, …; catalog `gene_annotations`), by symbol (`ait_nodes.GeneLookup`) — its `GeneAnnotation` IRI is the one the taxonomy's `curated_markers` link to; a gene it does not resolve continues through the cascade (HGNC / NCBIGene) and is flagged as not joining the markers | `concept_mappings.csv` (`review_loop map`) |
| species, brain region, any cell type | the ontologies of `trusted_ontologies/priority.md` (NCBITaxon, UBERON via CL, CL, …), on the row's `label_normalized` | `concept_mappings.csv` (`review_loop map`) |

So give every row a clean `label_normalized` (the name, not the surface: `subthalamic
nucleus`, not `subthalamic 139 nucleus`; `striatum`, not `striatal`), give gene rows their
species and `ncbi_taxon_id`, and extract every marker gene as its own gene row
(`ait_tables validate` fails otherwise). Never write an ontology IRI yourself.

---

## Pass 1 — Index and extract

### 1a. Build a retrieval index before extracting

Do not stream the raw paper into context repeatedly. Build the index once and reuse it
for every entity type:

- Segment the full text into sections, then into overlapping chunks of roughly 1,000
  tokens with 150 tokens of overlap. Keep `section_name`, `chunk_id`, and character
  offsets into the source text for each chunk.
- Include in the index: main text, figure and table captions, table bodies, methods,
  and all supplementary files you can retrieve.
- Store the index so later passes retrieve chunks by query rather than re-reading the
  paper. The character offsets are what makes the Pass 2 lexical check possible — do not
  discard them.

### 1b. Retrieve supplementary and deposited material

Before extracting, check for a taxonomy or cell type table published alongside the paper.
Look in this order and record what you find in `sources.csv`:

1. Supplementary tables and data files attached to the article.
2. Zenodo, figshare, and Dryad DOIs cited anywhere in the paper or data-availability
   statement.
3. GitHub or GitLab repositories named in data/code availability.
4. GEO, SRA, or NeMO accessions, and any `.h5ad`/`.loom` cell metadata they expose.

If a published taxonomy is found, treat its cell type labels as first-class extraction
input — they are usually more complete and more precisely spelled than the main text.
Record for each source whether it was retrieved successfully, and if not, why.

### 1c. Extract everything in a single sweep

Walk the indexed text once and extract all of the following together. Do not make
separate passes per entity type.

**Cell types** — any named cluster, class, or population an author treats as a distinct
biological entity:
- labels from established taxonomies (`L5 IT`, `PVALB`, `Sst Chodl`)
- labels the authors introduce (`Type 3 inhibitory neuron`, `cluster 14`)
- populations defined by marker expression, brain region, or technique
- cell types from the introduction or related work **only if** they are the subject of
  experimental evidence in this paper

Do not extract generic phrases containing "cell" that name no entity ("cell population",
"single cell", "cell type diversity"). When uncertain, extract it and set
`extraction_flag = uncertain`. Recall matters more than precision in this pass; filtering
happens in Pass 2.

**Genes** — any gene or gene product used to define, mark, or characterize a cell type:
marker genes, neurotransmitter-pathway genes, transcription factors, ion channels used
for classification. Record the symbol exactly as printed, plus a normalized symbol
(HGNC for human, MGI for mouse) when resolvable.

**Organisms / species** — species from which data were collected. Distinguish
experimental species from antibody host species and from citation-only mentions; record
the distinction in `species_role`.

**Brain regions** — as stated, with the acronym expanded where the paper gives it.

**Methods and assay metadata** — this is a required output, not an optional extra. For
each assay reported, capture what determines how much of the transcriptome was actually
observed, because that bounds how much weight a marker-based mapping can carry:
- assay/platform (e.g. 10x 3' v3, SMART-Seq v4, MERFISH, Patch-seq, Visium)
- **gene panel depth** — for targeted assays, the number of genes in the panel and the
  panel name or accession; for untargeted assays, record `untargeted`
- panel gene list location (supplementary table number, repository path) if available
- sequencing depth: median reads or UMIs per cell, median genes detected per cell
- number of cells or nuclei passing QC
- tissue preparation: cells vs nuclei, fresh vs frozen, post-mortem interval
- clustering method and software version; resolution parameter if stated
- cell type annotation method: de novo, label transfer, mapping to a named reference
  (record which reference and version)
- donor count, age, and sex where reported

**Evidence** — for every extracted entity, capture the verbatim supporting sentence(s)
together with `chunk_id` and the character offsets. Copy the span exactly: do not fix
typography, expand abbreviations, normalize whitespace, or join sentences with ellipses.
Offsets index the one text file the Pass 1a index was built on (main text, captions,
tables and supplements concatenated); keep that file, because Pass 2 verifies against it.
When an entity has several evidence sentences, the evidence columns are *aligned*
multi-values: the n-th element of `evidence_sentence`, `evidence_section`,
`evidence_chunk_id`, `evidence_char_start`, `evidence_char_end`, `evidence_verified` and
`evidence_similarity` all describe the same sentence. A literal `|` inside a quoted span
(a Markdown table row, say) is written `\|`.

---

## Pass 2 — Verification and provenance

### 2a. Lexical verification of evidence (mandatory, code-based)

Every evidence sentence must be checked programmatically against the source text. This
is not an LLM judgement — run the check:

```bash
python -m scripts.ait_evidence out/ --text paper.txt   # rewrites entities.csv + methods.csv, writes evidence_report.json
```

It implements the steps below exactly (case-sensitive; `methods.csv` has no offsets, so
a verbatim hit there is `exact`).

For each evidence row:
1. Normalize both the quoted span and the source text identically: Unicode NFKC, collapse
   runs of whitespace, normalize quote and dash characters, strip soft hyphens and
   line-break hyphenation.
2. Test for exact substring containment at the recorded offsets.
3. If that fails, search the whole document for the normalized span.
4. If that fails, compute the best fuzzy alignment and record the similarity score.

Write the result to `evidence_verified` using this vocabulary:

| Value | Meaning |
|---|---|
| `exact` | normalized span found verbatim |
| `exact_offset_corrected` | found verbatim, but not at the recorded offsets (offsets updated) |
| `fuzzy` | best alignment ≥ 0.95 similarity; record `evidence_similarity` |
| `not_found` | no alignment ≥ 0.95 |

Any entity whose only evidence is `not_found` is **quarantined**: it stays in the output
with `extraction_flag = unverified_evidence` and must not be promoted to a mapping edge
or written to the target knowledge graph. Report the count of quarantined entities; a non-trivial rate is a
signal that the extraction step is paraphrasing and the prompt needs tightening.

`fuzzy` evidence may support a mapping edge, but it is flagged: it never appears in
`review_sheet.csv` (which admits only `exact` and `exact_offset_corrected`), it does not
count toward evidence integrity, and `run_report.md` reports the number of edges whose
best evidence is only `fuzzy`, so a curator can audit them first.

### 2b. Deduplication

Collapse mentions that refer to the same entity within the paper. Keep the author's
preferred label as `label_verbatim` and move the rest to `aliases` (pipe-delimited).
Keep every distinct evidence sentence rather than only the first. Record `n_mentions` and
list the absorbed ids in `merged_mention_ids`. Never dedupe across papers — that is the
graph's job, not the extractor's.

### 2c. Provenance labels

Every row carries provenance for how its values were produced. Use a pipe-delimited list
drawn from this closed vocabulary, so a reviewer can see at a glance which fields a human
should audit:

| Label | Meaning |
|---|---|
| `code:pdf_parse` | value read directly from parsed document structure |
| `code:regex` | matched by a deterministic pattern |
| `code:lexical_verify` | confirmed by the Pass 2 substring check |
| `code:ontology_lookup` | resolved against HGNC/MGI/AIT/UBERON by API or file lookup |
| `code:supplement_table` | read from a supplementary or deposited table |
| `llm:extract` | produced by the extraction model |
| `llm:review` | adjudicated or corrected by a reviewing model pass |
| `human:curator` | set or corrected by a person |

Record provenance at field granularity in `field_provenance` as
`field=label[;label]` pairs, pipe-delimited, at minimum for `label_verbatim`,
`marker_genes`, `brain_region`, `species`, and `ait_node_id`. Also set `extractor_version`
and `run_id` on every row so a result set can be reproduced or retracted.

---

## Pass 3 — Mapping to AIT taxonomies

Map paper → AIT directly. Do not route through Cell Ontology; not all AIT types have CL
terms yet. Record a CL term in `cl_id` as an annotation only when one obviously applies;
when the matched node carries one in its trusted file (`cl_terms` from
`ait_nodes show`), use that.

Select the taxonomy first using species, brain region, and assay. Then find the best
match within it using label, marker genes, and hierarchy level.

### Available AIT taxonomies

Do not keep a list of taxonomies in this prompt. The list has one maintained home,
`trusted_ontologies/ait_taxonomies.json` (a dated snapshot of the brain-map.org taxonomy index,
kept next to the parsed taxonomy files),
and is read with code:

```bash
python -m scripts.ait_taxonomy list
python -m scripts.ait_taxonomy rank --species human --region "middle temporal gyrus"   # first --species = primary experimental species
python -m scripts.ait_taxonomy show AIT15.3
```

The ranking is a shortlist to adjudicate, not a decision. If no taxonomy in the snapshot
covers the paper's experimental species and region, record `skos_relation = none` with
that reason. Do not force the nearest taxonomy: a silent `closeMatch` to the wrong
taxonomy is worse than a visible failure. Write `ait_id` as the taxonomy's AIT accession
when the catalog has one (for a multi-species taxonomy, the number for the experimental
species), otherwise its CCN, otherwise its `taxonomy_name`; this is the `ait_id` that
`rank` prints. If the snapshot looks stale (a taxonomy the paper names is
missing), say so in `run_report.md` instead of inventing an ID.

### AIT nodes come from the trusted ontologies

Node data — labels, accession ids, hierarchy, marker genes — are the parsed Allen
taxonomies in `trusted_ontologies/`, the same directory every other trusted ontology
lives in (`concept_mapping.json` → `trusted_ontologies.directory`). The catalog entry's
`trusted_files` says which file holds which species (e.g. the Cross-Species Basal
Ganglia taxonomy: `Human_BG.ttl`, `Macaque_BG.ttl`, `Marmoset_BG.ttl`). They are
instances of the BICAN Cell Taxonomy model, `trusted_ontologies/cell_taxonomy.ttl`
(trusted priority 1 in `trusted_ontologies/priority.md`), and are read through it:

| Model class / property | What it gives the mapping |
|---|---|
| `bican:CellTypeTaxonomy` (`bican:title`, `bican:reference_genome`) | the taxonomy release a node belongs to |
| `bican:CellTypeSet` + `bican:order` | the hierarchy levels (Neighborhood → Class → Subclass → Group → cluster) → `ait_hierarchy_level` |
| `bican:CellTypeTaxon`: `rdfs:label`, `bican:accession_id` | `ait_cell_type_label`, `ait_node_id` |
| `bican:has_parent` | ancestors, for `nearest_ancestor_node_id` and ancestor `broadMatch` edges |
| `bican:curated_markers` (→ `bican:GeneAnnotation`, `biolink:symbol`), `gene` abbreviations of the label | the node's marker set (Pass 4) |
| `bican:Abbreviation` (`term`, `meaning`, `entity_type`, `skos:broadMatch`) | what each label token means: a gene, a region (UBERON/DHBA) or a cell type (CL) |
| `bican:cell_type_ontology_term`, `skos:broadMatch` to CL | `cl_id` |
| `bican:literature_name_short/long`, `bican:literature_support` | the names papers use for the node — a label match on these is `label_normalized` |

Read them with code, never from memory:

```bash
python -m scripts.ait_nodes info AIT19.5                     # file, genome, levels with node counts
python -m scripts.ait_nodes search AIT19.5 --label "D1 MSN" --gene DRD1 --region putamen [--level Group]
python -m scripts.ait_nodes show AIT19.5 CS20250428_GROUP_0049   # path, abbreviations, CL, markers
python -m scripts.ait_nodes markers AIT19.5 <node_id>... --out markers.json   # Pass 4 input
```

A per-species AIT number selects the species file (`AIT19.5` human, `AIT11.9` macaque,
`AIT101` marmoset); with a CCN or taxonomy name pass `--species`. `search` is a
shortlist to adjudicate, like `rank`. Rules:

- `ait_node_id` is the node's `accession_id` (`CS20250428_GROUP_0049`); a cluster-level
  node, which has none, uses its label (`Human-147`). `ait_cell_type_label` is the
  node's `rdfs:label` exactly as in the file, and `ait_hierarchy_level` its level.
  `scripts/ait_tables.py validate` rejects a node id or label the file does not have.
- Mark `ait_node_id` in `field_provenance` as `code:ontology_lookup` only when it came
  from `ait_nodes`, and name the file in `notes` (`trusted_ontologies/Human_BG.ttl`).
- A taxonomy with no `trusted_files` has no local node data. Take node ids only from a
  source the paper deposits or cites (`supplementary_mapping`, `author_statement`) and
  record it in `sources.csv`; otherwise record `skos_relation = none` with
  `no_match_reason = "no trusted node data for <ait_id>"`. Never fill node ids or
  labels from model knowledge.
- Cross-species: a mouse paper mapped to `AIT19.5` uses the human file and is at most
  `closeMatch` (house rule below). Pick the species file closest to the experimental
  species and say why in `notes`.

### Use SKOS match relations, with `match_confidence` derived from them

Every edge asserts a specific semantic relationship, recorded as a SKOS predicate in
`skos_relation`:

| `skos_relation` | Use when |
|---|---|
| `skos:exactMatch` | the paper entity and the AIT node are interchangeable in any context — same granularity, same referent |
| `skos:closeMatch` | same referent for practical purposes, but not guaranteed interchangeable across all contexts (e.g. the same subclass delineated slightly differently) |
| `skos:broadMatch` | **the AIT node is broader than the paper entity** — the paper describes a subset (paper `Sst Chodl` → AIT subclass `Sst`) |
| `skos:narrowMatch` | **the AIT node is narrower than the paper entity** — the paper describes a superset (paper "interneurons" → AIT cluster `Pvalb Vipr2_1`) |
| `skos:relatedMatch` | associated but neither equivalent nor hierarchically nested (e.g. an activity state, or a population defined by a transgenic line that cuts across types) |
| `none` | no defensible relation; `ait_node_id` blank and `no_match_reason` required |

**House rule: `exactMatch` is reserved for same-taxonomy identity.** SKOS `exactMatch`
is transitive, so a chain of them would fuse AIT nodes across taxonomies as the graph
grows. Use `skos:exactMatch` only when the paper's label *is* a node of the target
taxonomy. That means the paper annotated its cells against that taxonomy
(`author_statement`) or a deposited mapping table says so (`supplementary_mapping`), and
the experimental species is one the taxonomy covers. Everything else is `closeMatch`,
even when labels agree exactly: cross-taxonomy, cross-species, or the paper's own
clustering with matching names. `scripts/ait_tables.py validate` enforces this rule.

Direction is the thing people get wrong — read `broadMatch` as "the target is broader".
When no direct relation holds but an ancestor does, emit the ancestor edge as
`skos:broadMatch` rather than recording a failure.

`match_confidence` is kept for continuity with the existing review workflow, but it is
**derived deterministically** from `skos_relation` rather than judged independently — do
not set it by hand:

| `skos_relation` | `match_confidence` |
|---|---|
| `skos:exactMatch` | `exact` |
| `skos:closeMatch`, `skos:broadMatch`, `skos:narrowMatch`, `skos:relatedMatch` | `partial` |
| `none` | `none` |

The collapse is lossy in exactly the way that matters — `partial` hides whether the paper
entity was a subset or a superset of the AIT node — so read `skos_relation` when
interpreting an edge and `match_confidence` only for sorting a review queue. Fill it
with `python -m scripts.ait_tables derive out/`, which also copies
`cell_type_name_as_in_paper` and `species_experimental` from `entities.csv`. There is
deliberately no numeric mapping score: a number the model reports for itself gets read
as calibrated. Sort the queue by `match_confidence`, `evidence_verified` and the Pass 4
Jaccard values instead.

Record `basis_for_match` as a pipe-delimited subset of
`label_exact | label_normalized | marker_genes | brain_region | species | hierarchy_level | author_statement | supplementary_mapping`.
Record `mapping_evidence` — the specific markers or statement that carried the decision.
A mapping whose only basis is `label_exact` across a species boundary should be demoted
to `skos:closeMatch` and flagged in `notes`.

Where the paper itself states a mapping to a reference taxonomy ("cells were annotated by
label transfer to AIT15.3"), that is `author_statement` and is the strongest basis
available. Prefer it over your own label matching, and say so in `notes`.

---

## Pass 4 — Entity card and marker-gene diff

For each entity that reaches a mapping, assemble a card. The card's distinguishing
feature is the **marker-gene diff**, which makes the mapping auditable at a glance.

For the mapped AIT node, retrieve its marker gene set with
`python -m scripts.ait_nodes markers` (taxonomies with `trusted_files`): its
`curated_markers`, then the `gene` abbreviations of its own label, then those of its
ancestors (`--own-only` drops the ancestors). `curated_markers` are links to
`bican:GeneAnnotation` instances in the species' genome annotation file
(the one the taxonomy file names in `gene_annotation_version`, e.g. `trusted_ontologies/annotation-ENSEMBL-9606-109.ttl`);
the symbols are read from there. A marker the source taxonomy wrote as free text or
an unknown alias (e.g. `Six3 and Foxp1`, `Ctip2`) was left out when the taxonomy was
translated and is not in the file — do not add it back from memory. These sets are short — curated markers
exist for only a few dozen nodes per file — so a large `genes_paper_only` against them
is weak evidence against a mapping; say so in `notes`. Resolve both gene sets to a common
namespace (HGNC/MGI symbols, cross-species orthologs via the orthology tool) before
comparing, and record which namespace was used. Then emit, per entity, three sets:

| Set | Column | Meaning |
|---|---|---|
| Shared | `genes_shared` | in both the paper and the AIT node — supports the mapping |
| Paper-only | `genes_paper_only` | asserted in the paper, absent from the AIT marker set — either a novel marker or a sign the mapping is wrong |
| Taxonomy-only | `genes_taxonomy_only` | AIT markers the paper never mentions — expected to be large and is not evidence against the mapping |

Also report `n_shared`, `n_paper_only`, `n_taxonomy_only`, and `jaccard` (computed on the
resolved sets). `jaccard` is `NA` when the paper names no marker for the entity: an empty
paper side is no evidence either way, not a disagreement. Interpret these against the assay: a 300-gene MERFISH panel cannot
mention markers that are not in the panel, so weight `genes_taxonomy_only` by whether
each gene was measurable at all. Set `panel_limited = true` when the assay was targeted,
and compute `jaccard_panel_restricted` over the panel intersection as well.

An entity card is one row in `entity_cards.csv` carrying: the paper node, the entity, the
mapping edge with its SKOS relation, the three gene sets, the assay and panel depth, the
verified evidence sentence, and the provenance string. Join it to `mappings.csv` on
`mention_id`.

Build the cards with code once the marker sets are retrieved. Gene symbols are compared
exactly, so resolve orthologs before this step:

```bash
python -m scripts.ait_gene_diff out/ --markers markers.json --panels panels.json --namespace MGI
#   markers.json: {ait_node_id: [genes]}   panels.json: {gene_panel_name or assay_id: [genes]}
```

---

## Standardized output

Emit exactly these seven tables, with exactly these column names, in this order, then
`run_report.md` and the Turtle (`<paper_id>.ttl`, below). Column
names are a contract — do not rename, reorder, add, or drop columns; a field with no
value is left empty, not omitted. The machine-readable contract is
`schemas/ait-mapping-columns.json` (type, multi-valued flag, vocabulary and required
flag for all 147 columns). Start from it and finish by checking against it:

```bash
python -m scripts.ait_tables init out/            # header-only files
# ... Pass 1 extraction, then Pass 2a: python -m scripts.ait_evidence out/ --text paper.txt
python -m scripts.ait_tables derive out/          # match_confidence + crosswalk copies
python -m scripts.ait_gene_diff out/ --markers markers.json --namespace MGI
python -m scripts.ait_tables review-sheet out/    # the deterministic join
python -m scripts.ait_tables validate out/        # must exit 0 before anything reaches the knowledge graph
python -m scripts.review_loop map --mode ait out/ # concept_mappings.csv (trusted files first; genes: annotation first)
# ... judge panel and optional human feedback (Pass 5), then validate again
python -m scripts.ait_ttl out/ --text paper.txt --source paper.pdf   # ALWAYS: writes out/<paper_id>.ttl, must print VALID
```

Use UTF-8, comma-separated, quote all fields, `\n` line endings. Empty means not applicable; `NA`
means looked for and not found — the distinction matters downstream. Multi-valued fields
are pipe-delimited (`|`) with no surrounding spaces. Booleans are lowercase
`true`/`false`. Dates are ISO 8601.

### `paper.csv` — one row

`paper_id, doi, pmid, pmcid, title, journal, publication_date, first_author, last_author, corresponding_author, abstract, license, full_text_source, n_supplementary_files, taxonomy_deposited, taxonomy_deposit_url, extracted_at, extractor_version, run_id`

### `sources.csv` — one row per retrieved or attempted source

`paper_id, source_id, source_type, identifier, url, retrieved, retrieval_status, contains_cell_type_table, n_rows, notes`

`source_type` ∈ `main_text | supplementary | zenodo | figshare | dryad | github | geo | sra | nemo | other`

### `entities.csv` — one row per deduplicated entity

`paper_id, mention_id, entity_type, label_verbatim, label_normalized, aliases, n_mentions, merged_mention_ids, marker_genes, marker_genes_normalized, brain_region, brain_region_acronym, species, species_role, ncbi_taxon_id, assay_id, hierarchy_level, assertion_type, evidence_sentence, evidence_section, evidence_chunk_id, evidence_char_start, evidence_char_end, evidence_verified, evidence_similarity, extraction_flag, field_provenance, extractor_version, run_id`

- `entity_type` ∈ `cell_type | gene | species | brain_region`
- `species_role` ∈ `experimental | antibody_host | citation_only | reference_dataset`
- `hierarchy_level` ∈ `class | subclass | supertype | cluster | type | unspecified`
- `assertion_type` ∈ `asserted | inferred`
- `extraction_flag` ∈ `ok | uncertain | unverified_evidence | generic_phrase | duplicate_merged`

### `methods.csv` — one row per assay

`paper_id, assay_id, assay_name, platform, modality, targeted, gene_panel_name, gene_panel_size, gene_panel_source, median_reads_per_cell, median_umis_per_cell, median_genes_per_cell, n_cells_qc, n_nuclei_qc, preparation, species, brain_region, n_donors, donor_ages, donor_sexes, clustering_method, clustering_software_version, clustering_resolution, annotation_method, reference_taxonomy, evidence_sentence, evidence_chunk_id, evidence_verified, field_provenance, run_id`

- `modality` ∈ `transcriptomic | spatial | electrophysiological | morphological | epigenomic | proteomic | multimodal`
- `targeted` is boolean; when `true`, `gene_panel_size` is required

### `mappings.csv` — one row per edge (the reverse-lookup table)

The first ten columns after `paper_id` are fixed and must appear in this order; the
remainder carry the graph and provenance fields and follow them.

`paper_id, mention_id, cell_type_name_as_in_paper, species_experimental, ait_taxonomy_used, ait_id, ait_cell_type_label, ait_hierarchy_level, match_confidence, basis_for_match, notes, mapping_id, ait_node_id, skos_relation, mapping_evidence, no_match_reason, nearest_ancestor_node_id, cl_id, supersedes_mapping_id, mapped_by, field_provenance, run_id`

- `paper_id` leads because the paper is the node; it is the only column placed ahead of
  the fixed block
- `cell_type_name_as_in_paper` and `species_experimental` are copied verbatim from
  `entities.csv` on join, never re-derived — a mismatch between the two tables is a bug
- `species_experimental` carries only the species whose `species_role = experimental`
- `match_confidence` ∈ `exact | partial | none`, derived from `skos_relation` by the
  crosswalk in Pass 3
- `ait_cell_type_label` is the matched label; `ait_node_id` is its stable identifier and
  is what the reverse lookup traverses — populate both
- `notes` holds ambiguities, competing candidates, and demotion reasons
- `mapped_by` ∈ `code | llm | human`

### `review_sheet.csv` — flat curator view, one row per mapped cell type

A deterministic join of `entities.csv`, `methods.csv`, and `mappings.csv` for human
review. It introduces no new extraction: every value must already exist in a source
table, and a value appearing here but not there is a bug. Columns are fixed in this
order.

`cell_type_name, ait_match, ait_taxonomy_used, evidence_sentence, asserted_or_inferred, source_in_paper, brain_region, marker_genes, assay, species, hierarchy_level, notes, paper_id`

- `cell_type_name` ← `entities.label_verbatim`
- `ait_match` ← `mappings.ait_cell_type_label`; empty when `match_confidence = none`
- `evidence_sentence` ← `entities.evidence_sentence`, verbatim; only rows with
  `evidence_verified` in {`exact`, `exact_offset_corrected`} may appear
- `source_in_paper` ← `entities.evidence_section`, given as section name plus figure or
  table number where applicable (e.g. `Results; Fig. 3b`)
- `assay` ← `methods.assay_name` via `assay_id`
- `species` ← `entities.species` where `species_role = experimental`
- `notes` ← `mappings.notes`
- `paper_id` last, matching the existing sheet layout

Because this sheet omits `skos_relation`, do not use it to decide a mapping's
direction — it is a review surface, not the source of truth. `mappings.csv` is.

### Column crosswalk

The same string appears under different names in the normalized tables and the
cell-type-centric views. `entities.csv` keeps generic names because it also holds genes,
species, and regions:

| `entities.csv` | `mappings.csv` | `review_sheet.csv` |
|---|---|---|
| `mention_id` | `mention_id` | — |
| `label_verbatim` | `cell_type_name_as_in_paper` | `cell_type_name` |
| `species` (where `species_role = experimental`) | `species_experimental` | `species` |
| `hierarchy_level` | — (paper-side level) | `hierarchy_level` |
| `evidence_section` | — | `source_in_paper` |
| `assertion_type` | — | `asserted_or_inferred` |

### `entity_cards.csv` — one row per mapped entity

`paper_id, mention_id, cell_type_name_as_in_paper, ait_id, ait_node_id, ait_cell_type_label, skos_relation, match_confidence, gene_namespace, genes_shared, genes_paper_only, genes_taxonomy_only, n_shared, n_paper_only, n_taxonomy_only, jaccard, panel_limited, jaccard_panel_restricted, assay_name, gene_panel_size, evidence_sentence, evidence_verified, field_provenance, run_id`

### `run_report.md`

Counts per table; entities extracted, verified, quarantined (from `evidence_report.json`);
the marker comparison **as `marker_report.json` states it** (mapped nodes, which of them
carry taxonomy markers and which markers, edges where the paper names markers, edges with
shared markers, edges whose Jaccard is `NA`) — quote those numbers, never summarise
marker availability in your own words;
mappings by SKOS relation; edges whose best evidence is only `fuzzy`; unmapped entities
with reasons; sources attempted vs retrieved; the `ait_taxonomies.json` snapshot date
used; the trusted taxonomy file(s) nodes and markers were read from; the `validate` result; the Turtle file, its triple / entity / mention / AIT-edge / gene-link counts, the genes not linked, and its `validate_ttl` result (from `ait_ttl`); and every assumption you made that a curator should check.

---


### `<paper_id>.ttl` — the graph deliverable (always)

The tables are the review surface; the knowledge graph ingests Turtle. Every AIT run
ends with

```bash
python -m scripts.ait_ttl out/ --text paper.txt --source paper.pdf
```

which refuses tables that do not pass `ait_tables validate`, takes every entity's
ontology term from `concept_mappings.csv` (running the concept map stage itself, with the
same cascade, when that file is missing), writes
`out/<paper_id>.ttl` as instances of the Named Entity Ontology (the same
`json_to_ttl` builder as every NER run, so keys, UUIDv5 IRIs and provenance agree
with a `cns-cells` run of the same paper), and gates it with `validate_ttl` (a
failing file is renamed `.invalid.ttl` and is not a result). What it contains:

| Table | Becomes |
|---|---|
| `paper.csv` | `ner:Publication` + document version |
| `entities.csv` (not quarantined) | one `ner:NamedEntity` per row (`CellType`, `CellSubtype` for supertype/cluster/type, `Gene`, `Species`, `BrainRegion`), key = normalized label; mentions at exact offsets — inside the verified evidence spans, plus every other in-scope occurrence of a surface only that row uses |
| `mappings.csv` | per edge: `ner:resolvedToConcept` + `skos:<relation>` to the AIT node's own IRI from the trusted file (`urn:bkbit:…`, prefix `BKBIT`), and a `ner:ConceptMappingDecision` with basis, `mapping_evidence`, `notes` and the Pass 4 marker diff; `none` → a gap comment with `no_match_reason` |
| `concept_mappings.csv`, gene rows | `ner:resolvedToConcept` + `skos:exactMatch` to the gene's `GeneAnnotation` in the species' trusted genome annotation (`urn:bkbit:…`; catalog `gene_annotations`, e.g. human → `annotation-ENSEMBL-9606-109.ttl`). A match only by case or synonym is `skos:closeMatch`, flagged for review. A gene the annotation does not resolve (an unknown symbol, or a species without a trusted annotation) is mapped by the cascade (HGNC / NCBIGene) when it can be, with a note that it does not join the taxonomy markers; otherwise it is an unmapped row with the reason in `notes` |
| `concept_mappings.csv`, species / brain region / cell-type rows without an AIT edge | `ner:resolvedToConcept` + `skos:<match_tier>` to the row's `ontology_id` (trusted ontologies first: Species → NCBITaxon, BrainRegion → UBERON, CellType → CL > PCL). An unmapped row stays unmapped (internal `BRAINKB:` concept) and is counted as unmapped. A judge's or curator's correction of the row is what is written |
| `marker_genes` | `expresses` (RO:0002292) to the gene entity, only where one sentence of the paper names both (that sentence is the evidence); others are listed as not written |

Working files go to `out/.structsense/` (`<paper_id>_ait_final.json`,
`_ait_kg_plan.json`, `_ait_ttl_report.json`). In `run_report.md`, quote what
`ait_ttl` prints: the concept-mapping counts per class (mapped / unmapped), any trusted
files listed in `priority.md` but missing, the gene counts (linked to `GeneAnnotation` / mapped
elsewhere / unmapped), and every gene mapped elsewhere, not mapped, or linked by a weaker tier.

## Tools

- **Target knowledge graph** (where the results are stored) — query this *first*, when
  one is available. If the paper, or an entity with the same label and species, has
  already been extracted, retrieve the stored record instead of re-deriving it, and
  extend rather than duplicate. Reusing stored facts in place of re-reading source text
  is the intended steady state.
- **AIT taxonomy catalog** — `scripts/ait_taxonomy.py` over `trusted_ontologies/ait_taxonomies.json`
  picks the taxonomy (Pass 3).
- **AIT taxonomy reader** — `scripts/ait_nodes.py` over the trusted taxonomy files in
  `trusted_ontologies/` (model: `cell_taxonomy.ttl`): look up candidate nodes by label,
  region, or markers, and retrieve the marker gene set needed for the Pass 4 diff.
- **Deterministic stages** — `scripts/ait_evidence.py` (Pass 2a), `scripts/ait_tables.py`
  (derive / review sheet / validate), `scripts/ait_gene_diff.py` (Pass 4),
  `scripts/review_loop.py` / `scripts/record_judge.py` / `scripts/human_feedback.py`
  (Pass 5), `scripts/ait_ttl.py` (the Turtle, always last). Run them; do not reproduce
  their output by hand.
- **GFF / GeneOrthology tools** — normalize gene symbols and resolve cross-species
  orthologs before any gene-set comparison.
- **StructSense extraction skills**.

## Evaluation

Report recall and precision against the project's current gold-standard annotation set
for the paper, at three levels, and include the table in `run_report.md`:

1. **Entity recall** — fraction of gold cell types present in `entities.csv`, matched on
   normalized label with aliases counted as hits.
2. **Mapping accuracy** — of entities correctly extracted, the fraction whose
   `ait_node_id` matches gold. Score `skos_relation` separately: a correct node with the
   wrong relation direction is a distinct error class and should be counted as such.
3. **Evidence integrity** — fraction of evidence sentences with `evidence_verified` in
   {`exact`, `exact_offset_corrected`}. This is the trust metric and should be ~1.0; it is
   measured on every run, with or without a gold set.

Recall is the priority at this stage. Never discard an uncertain extraction to improve
precision — flag it with `extraction_flag = uncertain` and let the reviewer decide.
