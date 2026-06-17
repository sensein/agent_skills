# Multi-model NER ensemble (HuggingFace + LLM)

The skill ships an **ensemble extractor** that runs specialized HuggingFace NER models in parallel with the LLM extractor. Every mention carries a `source_model` field so the grouped view (`entities_grouped`) records which model(s) surfaced each entity. This mirrors the multi-model strategy structsense uses (`utils/ner_tool.py`).

## Why ensemble?

A single LLM extractor, even when prompted exhaustively and run with mask-recall, still misses domain-specific mentions that specialist models catch easily — and vice versa. Combining them:

- **HF specialists** are deterministic, fast, and trained on labeled biomedical corpora. They reliably catch high-frequency biomedical entities (genes, chemicals, diseases, anatomy).
- **LLM extractors** generalize better to surface forms not in any training set (novel cell-type names, atlas cluster IDs, jargon) and infer typed labels from context.
- **Together**, recall is higher than either alone, and the `consensus_count` (how many distinct models surfaced an entity) is a cheap, principled confidence proxy.

## Available profiles

Pick a domain-appropriate roster via `--ner-profile` on the CLI or `ner_ensemble_profile=...` from Python. Available profiles in `scripts/ner_models.py`:

| Profile | Models | When to use |
|---|---|---|
| `biomedical_broad` | d4data + BENT (Disease, Chemical, Gene, Organism) | General biomedical text. The recommended default. |
| `cns_cells` | BENT (Cell-Type, Cell-Line, Anatomical, Gene) + BioBERT-genetic + d4data | Cell atlases, patch-seq, scRNA-seq cell typing, CNS taxonomy work. |
| `pharmacology` | BC5CDR-chem + BENT (Chemical, Disease) + NCBI-disease | Drug/chemical/disease-focused work (pharmacology papers, clinical trial reports). |
| `genetic` | BioBERT-genetic + BENT (Gene) + d4data | Gene/protein-heavy text. |
| `clinical` | Clinical-AI-Apollo + blaze999 + d4data + BENT (Disease) | EHR notes, clinical case reports. |
| `minimal` | d4data + BENT (Gene, Disease) | Smallest sensible roster — fastest cold-start. |
| `all` | All of the above (the full default roster) | Maximum recall; longer warm-up while models load. |

Or pass an explicit list via `--ner-models hf/model-id-1,hf/model-id-2,...`.

## Default roster (== profile `all`)

| Model ID | Specialty |
|---|---|
| `d4data/biomedical-ner-all` | Broad biomedical |
| `mobashgr/BC5CDR-chem-WLT-384-BioELECTRA-Pubmed-ENS-20-5` | Chemicals (BC5CDR) |
| `mobashgr/NCBI-disease-WLT-256-SciBERT-13INS` | Diseases (NCBI-disease) |
| `alvaroalon2/biobert_genetic_ner` | Genes / proteins |
| `pruas/BENT-PubMedBERT-NER-Gene` | Genes (BENT family) |
| `pruas/BENT-PubMedBERT-NER-Chemical` | Chemicals |
| `pruas/BENT-PubMedBERT-NER-Disease` | Diseases |
| `pruas/BENT-PubMedBERT-NER-Anatomical` | Anatomy / brain regions |
| `pruas/BENT-PubMedBERT-NER-Cell-Type` | Cell types |
| `pruas/BENT-PubMedBERT-NER-Cell-Line` | Cell lines |
| `pruas/BENT-PubMedBERT-NER-Organism` | Species |
| `pruas/BENT-PubMedBERT-NER-Bioprocess` | Biological processes |

Plus the LLM extractor, which contributes items tagged `source_model: "llm_ner:<llm_model_string>"`.

## How it runs

1. The pipeline detects you've requested an ensemble (`--ner-profile` or `--ner-models`).
2. Each configured model is loaded in parallel via `concurrent.futures.ThreadPoolExecutor` (lazy: `transformers` is imported only when the ensemble is enabled). Models that fail to load are logged and skipped — never crash the run.
3. Each model emits its own mention list with `source_model` set.
4. The LLM extractor runs in parallel via the standard `prompts/extractor-ner-*.md` path and its items get `source_model: "llm_ner:<model>"`.
5. The four lists are concatenated (no dedup at this stage — the grouped view handles consolidation).
6. The grouped view (`entities_grouped`) collapses by `(entity.lower(), label)` and records:
   - `source_models`: sorted list of all contributing model IDs.
   - `source_model_counts`: how many mentions came from each model.
   - `consensus_count`: distinct number of contributing models.
7. Stats break down both the raw `entities[]` and the grouped view by `source_model` (see `stats.by_source_model`).

## Per-mention output shape (with `source_model`)

```jsonc
{
  "entity":         "BDNF",
  "label":          "Gene",
  "sentence":       "BDNF is upregulated in the hippocampus.",
  "start":          0,
  "end":            4,
  "paper_location": "Results",
  "source_model":   "alvaroalon2/biobert_genetic_ner",
  "source_score":   0.998,
  // populated by the alignment + judge stages …
  "ontology_id":    "http://identifiers.org/hgnc/1033",
  "ontology_label": "BDNF",
  "ontology":       "HGNC",
  "concept_mapping_provenance": "tool",
  "judge_score":    1.0
}
```

## Per-group output shape (in `entities_grouped`)

```jsonc
{
  "entity":         "BDNF",
  "label":          "Gene",
  "mention_count":  17,
  "source_models": [
    "alvaroalon2/biobert_genetic_ner",
    "d4data/biomedical-ner-all",
    "llm_ner:openrouter/anthropic/claude-sonnet-4-6",
    "pruas/BENT-PubMedBERT-NER-Gene"
  ],
  "source_model_counts": {
    "alvaroalon2/biobert_genetic_ner": 17,
    "d4data/biomedical-ner-all":        12,
    "llm_ner:openrouter/anthropic/claude-sonnet-4-6": 17,
    "pruas/BENT-PubMedBERT-NER-Gene":   15
  },
  "consensus_count": 4,
  "ontology_id":    "http://identifiers.org/hgnc/1033",
  "ontology_label": "BDNF",
  "ontology":       "HGNC",
  "judge_score_max": 1.0,
  "judge_score_avg": 0.95,
  "judge_score_min": 0.85,
  "sentences": [
    {"text": "BDNF is upregulated in the hippocampus.",
     "paper_locations": ["Results"]},
    {"text": "BDNF protein levels increase following exercise.",
     "paper_locations": ["Discussion"]}
  ],
  "mentions": [
    {"start": 0,   "end": 4, "source_model": "alvaroalon2/biobert_genetic_ner", "judge_score": 1.0, "...": "..."},
    {"start": 348, "end": 352, "source_model": "d4data/biomedical-ner-all",      "judge_score": 0.95, "...": "..."},
    "..."
  ]
}
```

`consensus_count=4` here means four distinct models agreed BDNF is an entity. Highly trustworthy. A `consensus_count=1` from only the LLM extractor is weaker signal — useful for low-frequency or novel mentions but worth checking.

## Stats break down by `source_model`

`stats.entities.by_source_model` reports how many raw mentions each model contributed:

```
ENTITIES:            1132 mentions, 287 unique (3.94x avg) [dropped: 0]
  by_label:          Gene=312, Protein=198, BrainRegion=87, Disease=72, …
  by_source_model:   alvaroalon2/biobert_genetic_ner=412,
                     llm_ner:openrouter/anthropic/claude-sonnet-4-6=287,
                     d4data/biomedical-ner-all=215,
                     pruas/BENT-PubMedBERT-NER-Gene=124,
                     pruas/BENT-PubMedBERT-NER-Disease=72,
                     pruas/BENT-PubMedBERT-NER-Anatomical=22
```

This is the diagnostic you reach for when yield feels wrong: which model is over- or under-contributing relative to expectations.

## Cost and latency

- **First run is slow** because HF model weights download. With the full default roster, expect 2–5 GB of downloads (cached locally afterwards).
- **Subsequent runs** are fast: weights load in 5–15 seconds; inference on a 30-page paper takes 1–3 minutes per model on CPU, or 5–15 seconds per model on a single GPU.
- **CPU vs GPU**: pass `--ner-device 0` (or any CUDA device index) to use a GPU. Default is `-1` (CPU).
- **`profile=minimal`** keeps cold-start under a minute and trades some recall for speed.

## When to skip the ensemble

- **Prototyping**: just the LLM extractor is fine. Skip `--ner-profile`.
- **Domain not covered by the available models**: e.g. astronomy, finance. Use only the LLM extractor.
- **CPU-only, latency-critical**: profile=minimal or skip.
- **Air-gapped without pre-downloaded model weights**: skip.

## When the ensemble doesn't help (and what to do)

| Symptom | Likely cause | Fix |
|---|---|---|
| `ensemble_models` shows all models skipped with "transformers not installed" | `pip install transformers torch` |
| All ensemble models contribute 0 items | Wrong domain (e.g. running biomedical models on news text) | Switch profile (`general`-only via LLM extractor) |
| Many duplicate mentions of the same span across models | Working as intended — `consensus_count` rises, `entities_grouped` consolidates them | No action; check `entities_grouped` not `entities` |
| `source_model` is missing on some entities | LLM extractor wasn't tagged. `pipeline.run()` calls `annotate_llm_provenance()` automatically; if you wired it manually, do that step. | — |

## Adding new models

To add a new HuggingFace token-classification model:

1. Verify it's a token-classification model (BERT/RoBERTa/ELECTRA family with an NER head). The runner uses `pipeline("ner", ...)` from `transformers`.
2. Add to `DEFAULT_HF_MODELS` in `scripts/ner_models.py`, or to a relevant profile.
3. If the model's raw labels differ from this skill's canonical taxonomy, add entries to `_LABEL_NORMALIZATION` so they map cleanly. Anything unmapped passes through verbatim.
4. The runner is generic; no per-model glue code is needed.

Sequence-classification or generative NER models aren't supported by the default runner — wrap them in a custom function and add to `jobs` in `run_ensemble`.
