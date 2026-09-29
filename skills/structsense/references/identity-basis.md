# Identity basis — what an entity is, and why it is that

For every cell-type mention (both the `cns-cells` and `neuroscience` variants), and
for other neuroscience entities when the text says what makes them that thing, the
extractor records two things:

```
CellTypeMention  ->  canonical cell type  +  identity evidence
```

Identity evidence is the set of biological characteristics that justify calling the span
that cell type — not a scientific claim. It follows the axes of Zeng 2022, *What is a cell
type and how to define it?* (Cell, doi:10.1016/j.cell.2022.06.031); see
`cell-type-definition-framework.md` and, for BICAN/HMBA taxonomies,
`bican-ait-crosswalk.md`.

```
Cell type = hierarchy + molecular identity + anatomical context
          + developmental context + morphology / connectivity / physiology
Hierarchy = class -> subclass -> type -> subtype   (HMBA: neighborhood/class/subclass/group)
Cell-type mapping = f(name, hierarchy, defining characteristics, context)
```

## The record (per occurrence — never copied to other occurrences)

```json
"identity_basis": {
  "canonical_candidate": "Pvalb GABAergic interneuron",
  "hierarchy_level": "subclass",
  "name_derivation": "marker_based",
  "stability": "type",
  "features": [
    {"kind": "hierarchy",        "value": "interneuron",      "role": "supporting", "source": "explicit_text"},
    {"kind": "molecular_marker", "value": "PVALB", "target": "Pvalb", "polarity": "positive",
     "role": "defining", "source": "explicit_text", "quote": "Pvalb-positive", "confidence": 0.9},
    {"kind": "neurotransmitter", "value": "GABA",             "role": "supporting", "source": "explicit_text", "quote": "GABAergic"},
    {"kind": "anatomical",       "value": "cortical layer 5", "role": "contextual", "source": "explicit_text", "quote": "layer 5"},
    {"kind": "anatomical",       "value": "motor cortex",     "role": "contextual", "source": "explicit_text", "quote": "motor cortex"}
  ]
}
```

The keyed form (`base_cell_type`, `molecular_marker: [...]`, `neurotransmitter`,
`anatomical_location: [...]`, with `support: explicit|context`) is accepted and
normalized to this (`scripts/identity.py`).

| field | values |
|---|---|
| `kind` | hierarchy, molecular_marker, neurotransmitter, transcriptomic, anatomical, morphological, electrophysiological, connectivity, developmental, functional, species, state |
| `role` | **defining** (the type is identified by it) · **supporting** (consistent, disambiguating) · **contextual** (where/when; not a reason) · **excluding** (a negative criterion) |
| `source` | explicit_text · surrounding_context · ontology_inference · naming_convention |
| `hierarchy_level` | neighborhood, class, subclass, supertype, type, subtype, group, cluster |
| `name_derivation` | marker_based, layer_projection, morphology_based, region_based, neurotransmitter_based, arbitrary_cluster, eponymous, mixed |
| `stability` | type, state, transient (a reactive astrocyte is a state, not a type) |

Roles are decided from the text, never assumed: a region is contextual unless the type is
region-defined ("striatal SPN", "L5 IT"); morphology is defining for a chandelier cell but
may only corroborate a transcriptomic type elsewhere; modalities do not always align.

## What the pipeline does with it

1. **Grounding** (`identity.grounded_mask`): a text-sourced feature stays only if its quote,
   target or value occurs in the occurrence's sentence or nearby text; others are dropped
   and counted (`identity_features_ungrounded`). The model's biology is never a source.
2. **Mapping** (`identity.refine_mappings`, after the name cascade): a name mapping a
   defining/supporting feature contradicts is rejected (GABA vs a glutamatergic CL term,
   via the CL hierarchy in `class_anchors.py`); where the name fails, the canonical
   candidate and compositions of defining features with the base type are tried
   ("Pvalb-positive GABAergic interneurons" -> CL:4023018 *pvalb GABAergic interneuron*,
   justified by PVALB, GABA, interneuron). Recorded as `identity_mapping`.
3. **Judging**: the mapping judge sees the identity and judges the mapping on it.
4. **Representation** (ontology 2.6.0): `EntityMention ner:hasIdentityBasis IdentityBasis`
   (canonicalCandidateLabel, identityHierarchyLevel, nameDerivation, typeStability) —
   `hasIdentityFeature` typed features (Molecular/Neurotransmitter/Anatomical/...
   IdentityFeature) with featureValue, featureRole, featureEvidenceSource, featurePolarity,
   featureConfidence, featureQuote and `featureEntity` (the extracted gene/region entity);
   `ConceptMappingDecision ner:justifiedByIdentityFeature / contradictedByIdentityFeature`.
5. **Questions**: CQ64 (why is it that type), CQ65 (which features justify the mapping),
   CQ66 (do papers define the same type the same way), CQ67 (named but never
   characterized).
