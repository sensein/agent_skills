# What counts as a cell type — and how the schema encodes it

Grounded in Zeng, *What is a cell type and how to define it?*, Cell 2022
([10.1016/j.cell.2022.06.031](https://doi.org/10.1016/j.cell.2022.06.031), PMC9342916).
This is the field's reference answer to the question, so the extraction layer should
encode **its** axes rather than invent a parallel vocabulary. Every axis below is a field
in `schemas/context-core.schema.json`.

The paper's own summary position: use the single-cell transcriptomic taxonomy as the
initial framework and anchor, then relate every other modality to it, and present the
result hierarchically rather than as a fixed count of types. Three consequences follow for
extraction, and they are why this layer is shaped the way it is.

## The seven axes

### 1. A definition has an anchor modality, and other modalities are related to it

Transcriptomics is the scaffold because it carries evolutionarily rooted molecular
signatures and supports label transfer across datasets; anatomy, physiology and
connectivity then refine it. An extraction must therefore record *which* modality
established the type and which merely characterized it — the same distinction as the
`defining` / `descriptive` role split, one level up.

→ `DefinitionRecord.anchor_modality`, `Feature.modality`
  ∈ `transcriptomic | epigenomic | spatial | morphological | physiological |
  connectional | developmental | functional | molecular_marker | unspecified`

### 2. Hierarchy, not a type count

Class → subclass → type → subtype, with divisions becoming more granular *and fuzzier*
toward the leaves. The paper is explicit that a hierarchical presentation is more
meaningful than fixing an exact number. So a definition carries its level, and two
definitions at different levels are `broader`/`narrower`, never `same_population`.

→ `DefinitionRecord.hierarchy_level` ∈ `class | subclass | supertype | type | subtype |
  cluster`, plus the HMBA vocabulary `neighborhood | class | subclass | group`

### 3. Discrete versus continuous variation

Lower levels of the hierarchy show more continuous variation, which is precisely why
cluster boundaries there are contested. A type reported as a gradient is not a failed
discrete type — it is a different kind of claim, and collapsing the two is how lumping
and splitting disputes get laundered into false precision.

→ `DefinitionRecord.variation_structure` ∈ `discrete | continuous_gradient | fuzzy |
  unspecified`

### 4. Type versus state — the axis that decides most linkage questions

A cell type can exist in multiple states; a cluster may represent a state rather than a
type. The paper's discriminating evidence is comparative: profile across timepoints,
behavioral, physiological or pathological conditions and see which clusters appear,
disappear or shift. It also offers a heuristic — transitions between states tend to be
continuous, while type switches tend to be abrupt — and notes that non-neuronal types in
particular show pronounced state changes (reactive astrocytes being the canonical case,
context-dependent and heterogeneous).

→ `DefinitionRecord.stability` ∈ `type | state | transient`
→ `DefinitionRecord.state_evidence` — how the call was justified: `cross_condition_comparison
  | timepoint_series | lineage_tracing | continuity_of_transcriptome | ieg_activation |
  perturbation | asserted_only`

`asserted_only` is the common value and should be visible: most papers call something a
type or a state without the comparative evidence that would settle it.

### 5. Regional scope

A type may be specific to a subregion, a region, or a whole brain structure; types may be
shared across cortical areas, and shared types often show gradient distribution or gradient
expression. "Cortical interneuron type X" is a different claim from "type X in V1".

→ `DefinitionRecord.regional_scope` ∈ `subregion | region | brain_structure |
  shared_across_regions | gradient | unspecified`

### 6. Developmental origin as identity

The glutamatergic/GABAergic split in cortex tracks developmental origin — glutamatergic
neurons generated within cortex, GABAergic neurons generated in the subcortical ganglionic
eminence and migrating in. Origin is therefore a *defining* basis, not context. This is
exactly what the HMBA `embedding_set` column (`LGE`, `MGE`, …) encodes.

→ `basis: "developmental_origin"`, `Feature.modality: "developmental"`

### 7. Cross-species homology is a level-scoped claim

Homology can hold at subclass level while the leaf types differ — the paper reports
one-to-one homology at subclass level between isocortex and hippocampal formation
glutamatergic types despite the types themselves being highly distinct. A homology claim
must therefore name the level at which it holds.

→ `LinkageCandidate.relation: "homologous"`, with `hierarchy_level` on both sides and
  `modality_congruence` recording whether the correspondence holds across modalities or
  only in one

## What this adds to the schema

| New field | Where | Why |
|---|---|---|
| `anchor_modality` | DefinitionRecord | which modality established the type |
| `modality` | Feature | Zeng's characterization axes, per evidence element |
| `hierarchy_level` | DefinitionRecord | class/subclass/type/subtype + HMBA levels |
| `variation_structure` | DefinitionRecord | discrete vs gradient vs fuzzy |
| `state_evidence` | DefinitionRecord | how a type/state call was justified |
| `regional_scope` | DefinitionRecord | subregion → structure, shared, gradient |
| `name_derivation` | DefinitionRecord | how the label was built (below) |
| `marker_class` | Feature | from the HMBA marker columns (below) |
| `modality_congruence` | LinkageCandidate | correspondence across modalities is variable, not assumed |

### `name_derivation` — labels are structured, and the structure is evidence

Cortical nomenclature is not arbitrary. GABAergic subclasses are named after canonical
marker genes (Lamp5, Sncg, Vip, Sst, Sst-Chodl, Pvalb); glutamatergic subclasses are named
by layer and projection class (L2/3 IT, L4/5 IT, L5 IT, L6 IT, Car3 IT, L5 ET, L5/6 NP,
L6 CT, L6b — IT intratelencephalic, ET extratelencephalic, NP near-projecting, CT
corticothalamic). Parsing this tells you what evidence the name itself asserts, and it is
recoverable without the paper saying so.

→ `name_derivation` ∈ `marker_based | layer_projection | morphology_based | region_based |
  neurotransmitter_based | arbitrary_cluster | eponymous | mixed`

A `marker_based` label asserts a marker; a `layer_projection` label asserts a location and
a projection target. Both should generate the corresponding `features[]` entries with
`role: "defining"` *and* a note that the evidence is the naming convention rather than a
measurement in this paper — which is what `detection: "stated_only"` is for.

### `marker_class` — the HMBA columns are four different claims

| HMBA column | `marker_class` | What it asserts |
|---|---|---|
| `tf_marker_genes` | `transcription_factor` | lineage/identity determinant — the closest thing to a core identity gene |
| `combo_markers` | `combinatorial` | only meaningful as a conjunction; set `criterion_logic` |
| `binary_markers` | `binary` | discriminative presence/absence |
| `curated_markers_to_primates` / `_to_mouse` | `cross_species_curated` | a homology claim, not a measurement |

Zeng's distinction between core gene sets maintaining type identity (master transcription
factors are the example given) and genes tied to functional states is exactly why
`transcription_factor` deserves its own value rather than being pooled into a marker list.

## Worked instance — an HMBA row through the framework

Taking `STRd D1 Matrix SPN` / `CS20250428_GROUP_0049`:

| Axis | Value |
|---|---|
| `hierarchy_level` | `group` (leaf), under subclass `STR D1 SPN` → class `CN LGE GABA` → neighborhood `Subpallium GABA` |
| `anchor_modality` | `transcriptomic` |
| `regional_scope` | `subregion` — STRd, with `STR:1.0` proportion in all four species |
| `variation_structure` | matrix vs striosome is a spatial compartment distinction → `discrete` if the paper separates them cleanly |
| `stability` | `type` |
| `state_evidence` | `asserted_only` unless the source compared conditions |
| `name_derivation` | `mixed` — region (STRd) + marker (D1) + compartment (Matrix) + type (SPN) |
| features | `DRD1` (`marker_class: combinatorial`, with `STXBP6`), `DRD1` again as `binary`; `criterion_logic: "f1 AND f2"` |
| `taxonomy_links` | `CS20250428_GROUP_0049` (`cell_set_accession`), `CL:4030043` (`ontology_term`) |
| literature | `He et al. 2021` → `defined_by`, with `rationale_for_literature_link` → `criterion_text` |

The sibling row `STRd D1 Striosome SPN` shares `DRD1` but differs in `KCNIP1` and the
compartment. Under the linkage rules these are `sibling` (disjoint children of
`STR D1 SPN`), **not** `same_population` — marker overlap on `DRD1` alone caps the relation
at `overlaps` (L4), and the shared parent resolves it to `sibling`. This is the case the
whole layer exists to get right: two rows, one shared marker, one shared parent, different
cells.

## Three extraction rules that follow

1. **Do not force a discrete type.** When a paper describes a gradient or a continuum, set
   `variation_structure: "continuous_gradient"` and leave the population as stated. A
   gradient forced into a discrete type is a fabricated boundary.
2. **Do not promote a state to a type.** `reactive astrocyte`, `activated microglia`,
   `IEG-high neurons` are states unless the paper shows the comparative evidence. Set
   `stability: "state"` and `state_evidence` honestly — `asserted_only` when that is all
   there is.
3. **Do not assume modality congruence.** A transcriptomic type and a morphological type
   with the same name correspond only if the paper shows it. Correspondence between
   transcriptomic types and other properties ranges from one-to-one to many-to-one, so
   record `modality_congruence` rather than merging.
