# Crosswalk — extracted definitions → AIT, CAS/BICAN, CL/PCL and gene annotation

What this answers: **does a BICAN taxonomy exist, what can you actually map a paper to,
which fields in the publication carry that information, and where does it fall short.**

Field names below were read from the primary schema files, not from memory:

- `AllenInstitute/AllenInstituteTaxonomy` → `schema/AIT_schema.csv` (AIT AnnData schema,
  v1.1.2 at time of writing) and `taxonomies.md`
- `cellannotation/cell-annotation-schema` → `general_schema.json` (CAS) and
  `BICAN_extension.json` (the BICAN profile)
- `brain-bican/taxonomy-development-tools` (TDT — the build/edit tooling)

## 1. Does a BICAN taxonomy exist?

Not as a single tree you can map to. What exists is a three-layer stack, and the layer you
target changes what "mapping" means:

| Layer | What it is | What you map to |
|---|---|---|
| **Format** — AIT | An AnnData (`.h5ad`) profile: required `obs` / `var` / `uns` / `obsm` keys for a cell-type taxonomy, with the CAS block carried in `uns.cellannotation_schema` | Nothing directly — it is the container |
| **Annotation schema** — CAS + BICAN extension | The record structure for what an annotation *is*: `labelsets[]` and `annotations[]`, with the BICAN extension adding `cell_set_accession`, hierarchy and annotation transfer | The record shape your extraction should produce |
| **Concrete taxonomies** | Individual published taxonomies converted to AIT and hosted for community mapping — Tasic 2018 mouse V1/ALM, Hodge 2019 human MTG, comparative LGN, Jorstad 2023 multiple cortical areas, Bakken 2021 human M1, Yao 2021 whole cortex + hippocampus, SEA-AD MTG, and others listed in `taxonomies.md` | The actual mapping target |

So "map to the BICAN taxonomy" resolves to: *pick a named taxonomy from that list (or
another CAS/AIT-formatted one), and link your extracted population to a cell set inside
it.* There is no universal identifier space that covers all of them; there are per-taxonomy
accessions plus ontology terms (CL, and PCL for types that have been ontologized) that
cross taxonomies.

Two independent identifier systems matter, and conflating them is the usual error:

- **`cell_set_accession`** (BICAN extension) — a per-taxonomy accession that stays stable
  when `cell_label` changes. Cluster identity *within* a taxonomy.
- **CL / PCL terms** (`cell_ontology_term_id`) — cross-taxonomy classes. PCL exists
  precisely because most data-driven clusters have no CL class.

A paper's cluster label is neither of these until someone links it.

## 2. Field-by-field crosswalk

### Document coordinates → AIT `uns` / `obs`

| Our field (`data_context`) | AIT target | Note |
|---|---|---|
| `reference_genome` | `uns.reference_genome` | Straight copy |
| `annotation_release` | `uns.gene_annotation_version` | **The GFF/GTF join key.** See §4 |
| `quantification_tool` | — | No AIT slot; keep in provenance |
| `atlas` | (governs `obs.brain_region_ontology_term_id`) | BICAN atlas region ids |
| `taxonomy_id` | the taxonomy's own identity / `uns.title`, `uns.dataset_purl` | |
| `mapping_tool` | `labelsets[].automated_annotation.algorithm_name` (+ `algorithm_version`, `algorithm_repo_url`) | CAS records the annotating algorithm |
| `reference_taxonomy` | `automated_annotation.reference_location`, or `transferred_annotations[].source_taxonomy` | |
| `data_accessions` | `uns.dataset_purl` | |

### Context frame → AIT `obs` columns

This is the closest thing to a 1:1 map in the whole crosswalk, and it is why the frame is
worth extracting at all:

| `context_frame` | AIT `obs` | Ontology AIT expects |
|---|---|---|
| `species` | `organism`, `organism_ontology_term_id` | NCBITaxon (child of NCBITaxon:33208) |
| `sex` | `self_reported_sex`, `self_reported_sex_ontology_term_id` | PATO (child of PATO:0001894) |
| `age_stage` | `development_stage` | free text in the current schema |
| `region` | `anatomical_region`, `anatomical_region_ontology_term_id` | UBERON |
| `atlas` + region | `brain_region_ontology_term_id` | brain-bican atlas ids |
| `assay` | `assay`, `assay_ontology_term_id` | EFO |
| `tissue_state` / `condition` | `disease`, `disease_ontology_term_id` | MONDO, or PATO:0000461 for normal |
| `preparation` | `suspension_type` (`cell` / `nucleus` / `na`) | partial — captures the dissociation, not the preparation |
| `strain_or_donor` | `donor_id` | identifier, not a descriptor |

`preparation` is only partly expressible: AIT distinguishes cells from nuclei but has no
slot for slice-vs-in-vivo-vs-organoid, because AIT describes transcriptomic taxonomies.
Keep the full value in our frame and write the reduced value to `suspension_type`.

### Definition → CAS / BICAN annotation

| Our field | CAS / BICAN field | Note |
|---|---|---|
| `population` | `cell_label` | Author's preferred label |
| (expanded form) | `cell_fullname` | Abbreviations expanded |
| `aliases` | `synonyms` | |
| `criterion_text` | `rationale` | CAS's rationale is free text — our verbatim quote is a strict improvement |
| (cited sources) | `rationale_dois` | |
| marker features, `role: defining`, positive polarity | `marker_gene_evidence` | Genes must exist in the matrix |
| `markers_negative` | `negative_marker_gene_evidence` | BICAN extension only |
| `taxonomy_links` (CL/PCL) | `cell_ontology_term_id`, `cell_ontology_term` | |
| `taxonomy_links` (accession) | `cell_set_accession` | BICAN extension |
| `broader` / parent definition | `parent_cell_set_accession` | Builds the hierarchy |
| `granularity` | `labelsets[].rank` | 0 = most specific |
| `defined_by` = this_study vs external | `labelsets[].annotation_method` (`manual` / `algorithmic` / `both`) | |
| `linkage_candidates` (to_external_taxonomy) | `transferred_annotations[]` → `transferred_cell_label`, `source_taxonomy`, `source_node_accession`, `algorithm_name`, `comment` | **The closest CAS analogue of our linkage record** |
| neurotransmitter feature | `neurotransmitter_accession`, `neurotransmitter_rationale`, `neurotransmitter_marker_gene_evidence` | BICAN extension |
| `context_frame`, `criterion_logic`, `threshold`, feature `role`/`detection` | `author_annotation_fields` | **No native slot — see §3** |

## 3. Where the crosswalk is lossy — state this, don't paper over it

1. **CAS has no experimental-frame slot at annotation level.** `obs` carries the frame
   per cell; an annotation record does not. A population defined only under an injury
   condition therefore has nowhere native to say so. Put it in `author_annotation_fields`
   and keep our frame as the authoritative copy.
2. **CAS has no general comparability record.** `transferred_annotations` is
   directional and shaped for algorithmic label transfer between taxonomies. It cannot
   express "these two populations overlap but are not the same", "not_comparable", or a
   curator's deferral. Our `linkage_candidates` is strictly richer; the CAS write is a
   projection that keeps only `same_population`-style links.
3. **`rationale` is prose, so `criterion_logic` and thresholds are lost on write.**
   "A and B" versus "A or B" survives only if you put it in `author_annotation_fields`.
4. **`marker_gene_evidence` has no role, polarity, level or detection.** Our feature
   distinctions — defining vs enriched, gene vs protein vs reporter allele — collapse on
   write. Keep the features; write the flattened list.
5. **A paper alone cannot produce a valid CAS/AIT file.** CAS annotations key to
   `cell_ids` in a matrix; `marker_gene_evidence` genes must be present in that matrix.
   Text extraction produces an *unanchored annotation stub* — correct in shape, missing
   its anchor. Say "CAS-shaped candidate record", never "CAS file".

## 4. The gene-annotation (GFF/GTF) join

AIT is explicit about this: `var.index` is the gene **symbol**, `var.ensembl_id` is the
stable id, and `uns.gene_annotation_version` records the annotation used during alignment.
The join from an extracted marker to a taxonomy's gene space is therefore:

```
marker symbol (verbatim, casing preserved)
  → gene_link.symbol
  → resolve against the GFF/GTF named by data_context.annotation_release
  → stable id  → match against var.ensembl_id in the AIT file
```

Each step can fail, which is why `gene_link.resolution_risk` is a required field:

- `symbol_only` — the normal case. The paper gives a symbol and no annotation release, so
  the symbol must be resolved against *some* release and the result carries that release's
  provenance, not the paper's.
- `ambiguous_symbol` — the symbol maps to more than one stable id in the chosen release.
- `deprecated_symbol` — resolvable only through an alias/withdrawn-symbol table; record
  which release retired it.

This is not hypothetical drift: `taxonomies.md` documents an AIT file reissued in August
2025 in which 70 Ensembl IDs changed, with downstream effects on mapping statistics and
markers. Two files, same taxonomy, different gene identifiers. A symbol resolved without a
declared release is a guess with a plausible-looking answer.

Practical consequence for extraction: never write `stable_id` from model knowledge (rule
T1/T5). Extract the symbol and the release; resolve downstream, in code, against a named
GFF/GTF, and record which one.

## 5. What is actually available in a publication

Ordered by how often you will find it:

| Available | Frequency | What it buys |
|---|---|---|
| Cluster/type labels | almost always | a candidate `cell_label` — no identity |
| Marker genes per type | usually (tables, figures) | `marker_gene_evidence`; the main matching signal |
| Species, region, assay | usually (Methods) | frame fields; hard discriminators for linkage |
| Reference genome | often | `uns.reference_genome` |
| **Gene annotation release** | **sometimes** | the GFF join; absent ⇒ `symbol_only` |
| An explicit correspondence sentence | sometimes | the only reliable cross-taxonomy identity a paper gives |
| Named reference taxonomy + mapping tool | sometimes | `transferred_annotations` provenance |
| `cell_set_accession` / CCN-style taxonomy id | rarely | direct join |
| CL / PCL terms | rarely | cross-taxonomy class |

The asymmetry drives the extraction design: the two fields that most often make a paper
joinable — `annotation_release` and the correspondence sentence — are each stated once,
in Methods or a single Results clause, and are missed by any per-sentence extraction pass.
That is why `data_context` is document-level and required, and why rule C11 makes the
correspondence sentence an explicit search target.

## 6. Mapping procedure

```
1. Establish the frame.  species, region, assay, preparation from data_context +
   definition_frame. A mismatch here ends the mapping: cross-species is a homology
   claim, not a lookup.
2. Declare the target taxonomy.  A named AIT/CAS taxonomy. Without one there is no
   accession space and the label stays dataset_local (rule T2).
3. Harvest the paper's own claims first.  taxonomy_links with asserted_by "paper".
   An explicit correspondence sentence outranks every similarity computation below.
4. Lexical candidates.  Match the population and its aliases against cell_label,
   cell_fullname and synonyms in the target. Candidates only — never an identity (C4).
5. Marker confirmation.  Intersect defining marker features with the candidate's
   marker_gene_evidence, after symbol resolution (§4). Agreement raises confidence;
   marker agreement ALONE caps the relation at "overlaps" (L4).
6. Frame check.  Region, species, assay, condition. Any conflict caps the relation at
   "overlaps" and confidence at 0.8 (L5/C5).
7. Granularity check.  Compare against labelsets[].rank. A label matching a class in
   one taxonomy and a subclass in another is broader/narrower, not same_population.
8. Emit a linkage_candidate, not a merge.  action "merge" needs same_population,
   confidence >= 0.9 and no conflicts (C6). In practice, text-only evidence almost
   never reaches that bar — expect link_related and defer_to_curator.
9. Ontology term last.  CL when a class fits; PCL when the type is data-driven and
   ontologized; otherwise unmapped with a reason. Never mint an IRI (T1).
```

Step 5 is where text-based mapping stops and data-based mapping begins. If you hold the
expression matrix, do not infer the correspondence from prose: run the real mapper
(MapMyCells / `scrattch.mapping`, per the AIT repo) and use the extracted definition as
the *audit trail* for what the paper claimed, compared against what the data says. Text
extraction answers "what did the authors assert"; it does not answer "which reference
cluster do these cells belong to".

## 7. Writing the extraction out

`definitions[]` + `data_context` → a CAS-shaped candidate record:

```
labelsets[]:   one per granularity level, rank from `granularity`,
               annotation_method from `defined_by`,
               automated_annotation from data_context.mapping_tool/reference_taxonomy
annotations[]: one per definition —
               cell_label, cell_fullname, synonyms, rationale (criterion_text),
               rationale_dois, marker_gene_evidence, negative_marker_gene_evidence,
               cell_ontology_term_id (only if the paper printed it),
               cell_set_accession (only if the paper printed it),
               parent_cell_set_accession from the in-paper hierarchy,
               transferred_annotations from linkage_candidates whose scope is
                 to_external_taxonomy and whose asserted_by is "paper",
               author_annotation_fields carrying: context_frame, criterion_logic,
                 thresholds, feature roles/detections/biotypes, resolution_risk,
                 and every linkage_candidate that is not a same_population claim
cell_ids:      ABSENT — this is a stub until attached to a matrix (§3.5)
```

Validate the round trip: everything written must be reconstructible from
`definitions[]`, and every identifier written must be greppable in the source text.

## 8. HMBA taxonomy sheets — the concrete target

The HMBA (Human and Mammalian Brain Atlas) annotation sheet is a flattened, four-level
version of the CAS model, and it is the most useful thing to aim an extraction at because
its columns say exactly what evidence the curators want. Accessions follow
`CS<YYYYMMDD>_<LEVEL>_<NNNN>` — e.g. `CS20250428_GROUP_0049`, `CS20250428_SUBCL_0028`,
`CS20250428_CLASS_0003`, `CS20250428_NEIGH_0002` — one per level, with a `CL:ID_<level>`
alongside where the type has been ontologized.

### Hierarchy

Four ranks, most specific first: **Group → Subclass → Class → Neighborhood**. In CAS terms
these are four `labelsets[]` with `rank` 0–3; in ours they are four definitions linked by
`broader`, each with its own `accession` and `CL:ID`. A type that is a Group in one release
and a Subclass in another is a `narrower`/`broader` linkage, never `same_population`.

### Column map

| HMBA column | Our field | Note |
|---|---|---|
| `Group` / `Subclass` / `Class` / `Neighborhood` | `definitions[].population` at four ranks | linked by `broader` |
| `accession_<level>` | `taxonomy_links[]`, `label_form: "cell_set_accession"` | **never minted** (T1) |
| `CL:ID_<level>` | `taxonomy_links[]`, `label_form: "ontology_term"`, registry CL/PCL | |
| `short_name_<level>`, `synonyms` | `aliases` | |
| `tokens_<level>` | — | derived from the label by the build, not extracted |
| `embedding_set` (e.g. `LGE`) | `data_context` / developmental origin feature | ties the group to a lineage |
| `spatial_regional_proportions`, `spatial_proportions_{human,macaque,marmoset}` | `evidence_kind: "proportion"` rows — one per species × region, value as written (`STR:1.0`) | in the full layer: a `region` feature with `parameters[]` `{name: "proportion", value: 1.0}` and `frame.species` |
| `spatial_description_manual` | `region` feature, `role: "descriptive"` | |
| `morphology_description` | `morphology` feature | |
| `ephysiology_description` | `ephys` feature | |
| `tf_marker_genes` | marker feature, `marker_class: "transcription_factor"` | |
| `combo_markers` | marker features + `criterion_logic` joining them with AND | the combination *is* the criterion |
| `binary_markers` | marker feature, `marker_class: "binary"` | presence/absence discriminative |
| `curated_markers_to_primates` / `curated_markers_to_mouse` | marker feature, `marker_class: "cross_species_curated"`, plus a `linkage_candidate` to that species' taxonomy | |
| `neurotransmitter` | BICAN `neurotransmitter_accession` + its marker evidence | |
| `literature_support`, `literature_name_short`, `literature_name_long` | `taxonomy_links[].evidence` + `defined_by` | the citation anchoring the type |
| `rationale_for_literature_link` | `criterion_text` / CAS `rationale` | |
| `display_order_*`, `color_hex_*`, `color_*` | — | presentation, assigned by the build |

### What a literature pipeline can and cannot fill

This division is the practical payoff of the whole layer:

**Fillable by reading papers** — `literature_support`, `literature_name_short/long`,
`rationale_for_literature_link`, `curated_markers_to_primates`, `curated_markers_to_mouse`,
`synonyms`, `spatial_description_manual`, `morphology_description`,
`ephysiology_description`, and candidate values for `tf_marker_genes` / `combo_markers` /
`binary_markers`.

**Not fillable from text** — `accession_*` (assigned by the taxonomy build, via TDT),
`CL:ID_*` (an ontology curation decision), `tokens_*`, `display_order_*`, `color_*`, and
the `spatial_proportions_*` numbers unless the paper prints them. Attempting any of these
from a paper is exactly the fabrication rule T1 forbids.

So the realistic output of an extraction run is a **candidate evidence sheet keyed by
label**, which a curator joins to accessions — not a filled taxonomy row.

### Two things the HMBA sheet forces on the model

1. **Marker kind is a real facet, not a detail.** CAS has one `marker_gene_evidence`
   field; HMBA has four columns that mean different things. A transcription-factor marker
   asserts lineage identity, a binary marker asserts discriminability, a combinatorial
   marker is only meaningful as a conjunction, and a cross-species curated marker is a
   homology claim. Flattening them loses the claim. Our `Feature.marker_class` carries the
   distinction: `transcription_factor | combinatorial | binary | cross_species_curated |
   enriched | canonical | unspecified`.
2. **Proportions are species-scoped region evidence.** `STR:1.0` in four species columns is
   four rows, not one — and a proportion stated for human that is absent for marmoset is
   a coverage gap, not a zero. Never fill a missing species column with 0.

A hygiene note on the example rows: the first row's `synonyms` field contains
`example|example`, which is template placeholder text rather than data. Extraction output
merged into a sheet like this should be validated against the placeholder vocabulary
before load, or the placeholders propagate as real synonyms.
