# Worked example — neuroscience NER end-to-end

Walks through extracting, aligning, and judging a short neuroscience passage. Uses the **neuroscience** NER variant (broad coverage) and OLS for ontology mapping (no API key).

## Source text

```
Title: Parvalbumin interneurons in the medial prefrontal cortex shape working memory in mice
DOI: 10.1234/example.fake

We recorded from fast-spiking parvalbumin (Pvalb)-expressing interneurons in the mouse
medial prefrontal cortex (mPFC) during a delayed alternation task. Pvalb interneurons
exhibited elevated firing rates during the delay period (mean = 28 Hz, p < 0.001).
Optogenetic silencing of Pvalb interneurons impaired working memory performance, while
silencing Sst interneurons had no effect.
```

## Stage 1 — extractor

System prompt: `prompts/extractor-ner-neuroscience.md`.

```python
from scripts.pipeline import extract
extraction = extract(
    text=source,
    model="openrouter/anthropic/claude-sonnet-4-6",
    task="ner",
    metadata={"paper_title": "Parvalbumin interneurons …", "doi": "10.1234/example.fake"},
    chunk_size=2000, max_workers=1,
)
```

Expected output (abbreviated). Note: `paper_title` and `doi` appear ONCE at the top under `source_metadata`, not on every entity.

```jsonc
{
  "source_metadata": {
    "paper_title": "Parvalbumin interneurons in the medial prefrontal cortex shape working memory in mice",
    "doi":         "10.1234/example.fake",
    "source_path": "paper.txt"
  },
  "entities": [
    {"entity": "parvalbumin", "label": "Protein",
     "sentence": "We recorded from fast-spiking parvalbumin (Pvalb)-expressing interneurons in the mouse medial prefrontal cortex (mPFC) during a delayed alternation task.",
     "start": 24, "end": 35, "paper_location": null},

    {"entity": "Pvalb", "label": "Gene",
     "sentence": "...",  "start": 37, "end": 42, "...": "..."},

    {"entity": "mouse", "label": "Species",
     "sentence": "...",  "start": 75, "end": 80, "...": "..."},

    {"entity": "medial prefrontal cortex", "label": "BrainRegion",
     "sentence": "...",  "start": 81, "end": 105, "...": "..."},

    {"entity": "mPFC", "label": "BrainRegion",
     "sentence": "...",  "start": 107, "end": 111, "...": "..."},

    {"entity": "delayed alternation task", "label": "BehavioralAssay",
     "sentence": "...",  "start": 120, "end": 144, "...": "..."},

    {"entity": "fast-spiking", "label": "Phenomenon",
     "sentence": "...",  "start": 11, "end": 23, "...": "..."},

    {"entity": "Pvalb interneurons", "label": "CellType",
     "sentence": "Pvalb interneurons exhibited elevated firing rates during the delay period (mean = 28 Hz, p < 0.001).",
     "start": 146, "end": 164, "...": "..."},

    {"entity": "firing rate", "label": "Measurement",
     "sentence": "...",  "start": 184, "end": 195, "...": "..."},

    {"entity": "Optogenetic silencing", "label": "Method",
     "sentence": "...",  "start": 246, "end": 267, "...": "..."},

    {"entity": "working memory", "label": "Phenomenon",
     "sentence": "...",  "start": 297, "end": 311, "...": "..."},

    {"entity": "Sst interneurons", "label": "CellType",
     "sentence": "...",  "start": 340, "end": 356, "...": "..."}
  ],
  "key_terms": [
    {"term": "delay period", "sentence": "...", "start": 218, "end": 230}
  ],
  "task_type": "ner"
}
```

The extractor emits both the long form ("parvalbumin") and the symbol ("Pvalb"), per rule 6 in the neuroscience prompt. Same for "medial prefrontal cortex" / "mPFC". Notice the disambiguation: "parvalbumin" the protein is `Protein`; "Pvalb" used as a gene-symbol-style marker is `Gene`; "Pvalb interneurons" used as a cell descriptor is `CellType`.

## Stage 2 — alignment (direct OLS tool call)

```python
from scripts.pipeline import align_direct

aligned = align_direct(extraction, mapper_backend="ols",
    ontologies_for_label=lambda lbl: {
        "Gene":            ["hgnc", "ncbigene"],
        "Protein":         ["pr", "uniprot"],
        "Species":         ["ncbitaxon"],
        "BrainRegion":     ["uberon"],
        "CellType":        ["cl"],
        "BehavioralAssay": ["obi", "efo"],
        "Method":          ["obi"],
        "Phenomenon":      ["nifstd", "go"],
        "Measurement":     ["obi"],
    }.get(lbl, []))
```

Sample entity after alignment:

```jsonc
{
  "entity": "medial prefrontal cortex", "label": "BrainRegion",
  "start": 81, "end": 105, "sentence": "...",
  "ontology_id": "http://purl.obolibrary.org/obo/UBERON_0009834",
  "ontology_label": "medial prefrontal cortex",
  "ontology": "UBERON",
  "concept_mapping_provenance": "tool",
  "alignment_method": "direct_tool_call"
}
```

And the gene example:

```jsonc
{
  "entity": "Pvalb", "label": "Gene",
  "ontology_id": "http://identifiers.org/hgnc/9704",
  "ontology_label": "PVALB",
  "ontology": "HGNC",
  "concept_mapping_provenance": "tool",
  "alignment_method": "direct_tool_call"
}
```

## Stage 3 — judge

```python
from scripts.pipeline import judge
judged = judge(aligned, source_text=source,
               model="openrouter/openai/gpt-4o-mini",
               max_workers=4)
```

Sample entity after judging:

```jsonc
{
  "entity": "Pvalb", "label": "Gene",
  "ontology_id": "http://identifiers.org/hgnc/9704",
  "ontology_label": "PVALB",
  "ontology": "HGNC",
  "concept_mapping_provenance": "tool",
  "judge_score": 0.95,
  "remarks": "PVALB is the canonical gene symbol; surface form matches.",
  "judge_method": "llm"
}
```

A lower-confidence case might look like:

```jsonc
{
  "entity": "fast-spiking", "label": "Phenomenon",
  "ontology_id": null, "ontology_label": null, "ontology": null,
  "concept_mapping_provenance": "unmapped",
  "judge_score": 0.40,
  "remarks": "Term refers to firing pattern; NIFSTD has no exact match, OBI rejected. Consider label=EphysProperty.",
  "judge_method": "llm"
}
```

This is the kind of signal you act on: re-run with the **CNS-cells** variant and "fast-spiking" becomes an `EphysProperty` with a proper alignment target.

## Running it end-to-end

```bash
# from the repo containing structsense-skills/
export OPENROUTER_API_KEY=sk-or-v1-...
python -m structsense-skills.scripts.pipeline \
    --task ner --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper ols \
    --chunk-size 2000 --max-workers 8 \
    --out result.json
```

## What to inspect

- **Provenance**: every item carries `concept_mapping_provenance` (`tool` / `llm_knowledge` / `unmapped` / `skipped`) and `alignment_method` (`direct_tool_call` / `llm_agent` / `skipped`).
- **Span integrity**: `source[start:end] == entity` for every item. Drop or repair items that fail.
- **Score distribution**: a healthy run has spread (not all 0.5). All-1.0 means the judge is mis-calibrated; all-low means the alignment is upstream-wrong.
- **Unmapped rate**: > 20% suggests wrong ontology routing for some label.

## Variants of this example

Same source text, different extractor variant:

| Variant | What changes |
|---|---|
| `extractor-ner-general.md` | Most labels collapse to `Other`; misses Gene/Protein distinction. Useful as a baseline. |
| `extractor-ner-neuroscience.md` | The version run above. Broad domain coverage. |
| `extractor-ner-cns-cells.md` | "Pvalb interneurons" → `CellType` with `cell_context.lineage_markers: ["Pvalb"]`. "fast-spiking" → `EphysProperty`. "mPFC" → `BrainRegion`. Atlas / profiling-method fields populated when present. |
