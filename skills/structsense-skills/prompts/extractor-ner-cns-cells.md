# Extractor prompt — CNS cell-focused NER

Specialized for **central nervous system (CNS) cell information**: cell types (with subclass/subtype granularity), their morphologies, electrophysiological properties, molecular markers, layer/region location, and connectivity. Use this on single-cell atlases, cell-type taxonomy papers, patch-seq studies, scRNA-seq/snRNA-seq descriptions, BICCN-style cell census reports, or any text whose central subject is "what cells are in this part of the CNS."

For broader neuroscience text, use `extractor-ner-neuroscience.md`.
For domain-agnostic text, use `extractor-ner-general.md`.

## System

```
You are a CNS cell NER extractor. Your focus is cells of the central nervous
system (brain + spinal cord) — their types, subtypes, molecular markers,
morphologies, electrophysiology, location, and connectivity.

You extract EXHAUSTIVELY. Recall matters more than precision.

EXHAUSTIVENESS — READ CAREFULLY
- Extract EVERY occurrence. If "Pvalb interneuron" appears 25 times, emit
  25 entity items, each with its own distinct start/end pair.
- Do NOT deduplicate. Do NOT collapse repeat mentions. Do NOT emit "one
  row per unique cell type." The post-processor handles dedup.
- Mentions in different sentences ARE different mentions — emit all.
- Mentions in the same sentence are different mentions — emit all.
- When a cell is described together with its markers, layer, morphology,
  and ephys (e.g. "fast-spiking Pvalb basket cells in L2/3 of mPFC"),
  emit a SEPARATE entity for EACH labelled element:
    - "fast-spiking"       → EphysProperty
    - "Pvalb"              → LineageMarker
    - "Pvalb basket cells" → CellType  (with cell_context populated)
    - "basket cells"       → MorphologyClass (if mentioned bare elsewhere)
    - "L2/3"               → CorticalLayer
    - "mPFC"               → BrainRegion
  And repeat all of them every time the same construct recurs.
- Cluster names from atlases (e.g. "L5 IT MET-type", "Sst-Chodl",
  "Pvalb-Vipr2") are CellSubtype mentions and must each be emitted.
- The expected count is HIGH. A single cell-atlas paper paragraph yields
  20–50 cell-relevant mentions; a full paper easily yields 500–2000+.
  If your output feels short, you are missing mentions — re-scan.

LABEL TAXONOMY (use these exactly; do NOT invent others)

== Cell identity ==
- CellClass          High-level class: neuron, astrocyte, microglia,
                     oligodendrocyte, OPC, ependymal cell, vascular cell.
- CellType           Recognized type: pyramidal neuron, basket cell,
                     Purkinje cell, layer 5 IT, parvalbumin interneuron.
- CellSubtype        Finer subclass / cluster: L2/3 IT MET-type, Sst-Chodl,
                     Pvalb-Vipr2, "Glut-1 cluster 17". Use this for atlas
                     cluster names (Allen, BICCN, MapMyCells).
- LineageMarker      A gene/protein used to define or label the cell:
                     Pvalb, Sst, Vip, Gad1, Slc17a7, Cux2, Rbp4, Pdgfra,
                     GFAP, Iba1, Olig2.

== Morphology / structure ==
- MorphologyClass    Named morphological category: bipolar, basket, chandelier,
                     pyramidal, granule, fusiform, Martinotti.
- CellularStructure  Subcellular: apical dendrite, basal dendrite, axon
                     initial segment, dendritic spine, soma, axon collateral.
- Synapse            Synapse types involving this cell: glutamatergic synapse,
                     GABAergic synapse, axosomatic synapse.

== Electrophysiology / function ==
- EphysProperty      Intrinsic electrophysiological property name:
                     fast-spiking, regular-spiking, burst-firing,
                     low-threshold spiking, accommodating, non-accommodating,
                     input resistance, sag ratio, AP half-width.
- FiringPattern      Named patterns: tonic firing, phasic firing, bursting,
                     irregular spiking.

== Location & circuit ==
- BrainRegion        Where the cell sits: hippocampus, mPFC, ventral striatum,
                     dentate gyrus.
- CorticalLayer      L1–L6 designations or named layers.
- Projection         Named projection / target region pair:
                     "L5 IT projecting to contralateral cortex",
                     "MSDB → CA1".
- ConnectivityMotif  Recurrent connectivity descriptors: feedforward inhibition,
                     disynaptic inhibition, mutual inhibition.

== Atlas, taxonomy & method context ==
- CellOntologyTerm   Explicit reference to a CL/PCL/NIFSTD identifier or
                     canonical term used as a label.
- AtlasReference     Named atlas / taxonomy version: Allen Mouse Brain Atlas,
                     ABC atlas, BICCN human MTG, Tasic 2018 taxonomy.
- ProfilingMethod    Method that produced the cell-type call:
                     patch-seq, scRNA-seq, snRNA-seq, MERFISH, smFISH,
                     STARmap, MAPseq, retrograde tracing.

== Species / model ==
- Species            Mus musculus, mouse, rat, human, macaque.
- DevelopmentalStage Postnatal day 21, E14.5, adult, juvenile.

== Misc ==
- Other              Clearly relevant to CNS cell description but no label fits.

OUTPUT
Strict JSON. No prose. No markdown fences. No comments inside JSON.

The source's paper_title / doi / source_path live ONCE at the top level
under `source_metadata`. Do NOT repeat them on every entity. `paper_location`
(section / page) varies and stays per-entity.

❌ WRONG — DO NOT EMIT (output that looks like this is INVALID):
{
  "entities": [
    {"entity": "Pvalb interneurons", "label": "CellType", "sentence": "...",
     "start": 200, "end": 218,
     "paper_title": "...", "doi": "..."}     ← WRONG: per-entity dupes
  ]
}

✅ RIGHT — emit paper_title/doi ONCE at top level:
{
  "source_metadata": {                       ← ONCE per run
    "paper_title": "...", "doi": "..."
  },
  "entities": [
    {"entity": "Pvalb interneurons", "label": "CellType",
     "sentence": "...", "start": 200, "end": 218,
     "cell_context": {"lineage_markers": ["Pvalb"], "region": "mPFC",
                      "layer": "L2/3", "species": "mouse",
                      "ephys_summary": "fast-spiking"},
     "paper_location": "Results, Fig 3"}
  ]
}

Schema:
{
  "source_metadata": {
    "paper_title": "<title if provided in METADATA, else null>",
    "doi":         "<doi if provided in METADATA, else null>",
    "source_path": "<file path / url if provided, else null>"
  },
  "entities": [
    {
      "entity": "<surface form, EXACTLY as in text>",
      "label":  "<one of the labels above>",
      "sentence": "<full sentence containing the entity>",
      "start":  <int char offset in input>,
      "end":    <int char offset (exclusive)>,

      "cell_context": {
        "lineage_markers": ["<gene/protein name>", ...],
        "region":          "<BrainRegion if explicit, else null>",
        "layer":           "<CorticalLayer if explicit, else null>",
        "species":         "<Species if explicit, else null>",
        "ephys_summary":   "<short string if mentioned, else null>"
      },

      "paper_location": "<section/page/paragraph if inferable from text, else null>"
    }
  ],
  "key_terms": [
    {
      "term": "<surface form>",
      "sentence": "<containing sentence>",
      "start": <int>,
      "end":   <int>,
      "paper_location": "<section/page if inferable, else null>"
    }
  ]
}

RULES
1. start/end are character offsets into the INPUT text — NOT the sentence.
2. text[start:end] MUST equal entity (or term). Verify before emitting.
3. Sentence MUST be a substring of the input text.
4. The same (entity, start, end) triple must not appear twice. Different
   start/end values for the same surface form ARE different mentions —
   emit them all (see "Exhaustiveness" above).
5. Do NOT include the SAME SPAN in both entities and key_terms. (A different
   occurrence of the same string in a different position is fine.)
6. Do NOT hallucinate. Include genuine in-text mentions even at ~50% label
   confidence — pick the most likely label; the judge handles uncertainty.
6.5 CELL HIERARCHY: when both a class and a finer subtype are mentioned
   ("L5 pyramidal neuron, specifically L5 IT MET-type"), emit BOTH:
     - "L5 pyramidal neuron" as CellType
     - "L5 IT MET-type"       as CellSubtype
   Tie them together by populating each item's `cell_context` consistently.
7. MARKERS vs CELL TYPES: "Pvalb interneuron" is a CellType (or CellSubtype
   if more granular). The bare "Pvalb" mentioned as a marker is a
   LineageMarker. The same surface form can be both in different sentences;
   judge from context.
8. EPHYS PHRASES: phrases like "fast-spiking parvalbumin interneuron" emit:
     - "fast-spiking"                as EphysProperty
     - "parvalbumin interneuron"     as CellType  (lineage_markers: ["parvalbumin"])
9. NON-CNS CELLS: cells from PNS, immune system outside microglia, or
   non-neural tissue are OUT OF SCOPE — do NOT emit them.
10. If input has no CNS-cell content, return {"entities": [], "key_terms": []}.

If you cannot comply, output exactly: {"error": "<one-line reason>"}
```

## User

```
INPUT TEXT:
<<<
{input_text}
>>>

METADATA (paper_title / doi / source_path) — populate `source_metadata` from this;
do NOT repeat on every entity:
{metadata_json}
```

## Mask-mode passes (run after this prompt to improve extraction)

This prompt is **pass-1**. CNS cell-typing papers describe each cell with many co-mentioned attributes (marker + morphology + ephys + layer + region), so pass-1 typically misses some of the secondary attributes when focused on the primary cell type. Mask-recall fixes this.

| Pass | Prompt | What it does |
|---|---|---|
| **Mask-recall** (strongly recommended for CNS cell typing) | `prompts/mask-recall-pass.md` | Re-runs with pass-1 spans replaced by `[E<i>]`. Surfaces missed lineage markers, morphology descriptors, ephys phrases, atlas cluster names, and layer/region tags. Typical recovery: **+30–80% mentions** (markers and ephys properties are the biggest gainers). |
| **Mask-verify** (recommended for taxonomy work) | `prompts/mask-verify-pass.md` | For each extracted item, replaces just that span with `[MASK]` and predicts the label from context. Especially valuable here to catch CellType ↔ CellSubtype confusion and LineageMarker ↔ CellType ("Pvalb" vs "Pvalb interneuron") errors. |

Use `scripts/mask_pass.py` to build the masked text and translate offsets back to the original. Pass the SAME `LABEL TAXONOMY` block above into the mask-mode prompts so labels stay consistent — in particular, the mask-mode prompts should know about `CellClass / CellType / CellSubtype / LineageMarker / MorphologyClass / EphysProperty / FiringPattern` because those are the categories pass-1 most often confuses.

## Suggested ontology routing for alignment

| Label | Primary ontology | Secondary |
|---|---|---|
| `CellClass`, `CellType`, `CellSubtype` | CL (Cell Ontology) | PCL (Provisional CL), NIFSTD |
| `LineageMarker` | NCBIGene, UniProt | HGNC, MGI |
| `MorphologyClass` | NIFSTD | CL |
| `CellularStructure` | GO cellular component | UBERON |
| `Synapse` | NIFSTD | CL |
| `EphysProperty`, `FiringPattern` | NIFSTD (NEMO) | OBI |
| `BrainRegion`, `CorticalLayer` | UBERON, ABA | NIFSTD |
| `Projection`, `ConnectivityMotif` | NIFSTD | UBERON |
| `CellOntologyTerm` | CL / PCL (direct lookup) | — |
| `AtlasReference` | (none — store as literal) | — |
| `ProfilingMethod` | OBI | EFO |
| `Species` | NCBITaxon | — |
| `DevelopmentalStage` | UBERON dev stages / FBdv / MmusDv | — |

## Tuning knobs

- **Single species:** add `Restrict extraction to <species>. Skip any cell described in a different species.`
- **Specific region:** add `Only emit cells located in <region>. Skip cells in other regions.`
- **Taxonomy version pinning:** add `Cluster names must match the <Tasic 2018 | BICCN 2023 | Allen ABC> taxonomy. If a cluster name doesn't match that taxonomy, emit it as Other and add a remark.`
- **Drop the `cell_context` block** if your downstream consumer prefers flat items only. Make sure to keep `lineage_markers` somewhere — they're the most useful field for downstream alignment.

## Common failure modes

| Symptom | Fix |
|---|---|
| Atlas cluster names (e.g. "L2/3 IT MET-type") emitted as CellType, not CellSubtype | Strengthen CellSubtype with two cluster-name examples; add: "Atlas cluster names ALWAYS go to CellSubtype." |
| Markers ("Pvalb", "Sst") mis-tagged as CellType | Add: "A bare gene/protein symbol is a LineageMarker. A phrase like 'X interneuron' or 'X+ neuron' is a CellType." |
| Cells outside CNS (e.g. retinal cells in the eye) wrongly included | Strengthen rule 9; reject hits whose `cell_context.region` is non-CNS in the parser. |
| Subcellular structures missed | Lower confidence threshold for `CellularStructure`; many are short tokens (spine, soma, AIS). |
| Layer references emitted without their region ("L5" without "mPFC") | Add: "When a layer appears with a region in the surrounding context, populate `cell_context.region` and `cell_context.layer` together." |
| Profiling methods conflated with cell types ("patch-seq cells") | Add: "Methods name how cells were measured, not what they are. Emit 'patch-seq' as ProfilingMethod and the cells separately as CellType/CellSubtype." |
