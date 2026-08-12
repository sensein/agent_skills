"""Ensemble HuggingFace NER extractors with per-mention provenance.

Mirrors the multi-model strategy structsense uses (`ner_tool.py`): run
several specialized biomedical NER models in parallel alongside the LLM
extractor, then merge their outputs. Every emitted item carries a
``source_model`` field so the grouped view can report which models
surfaced each entity (and downstream consumers can filter by model).

Default model roster
--------------------
    d4data/biomedical-ner-all                             — broad biomedical
    mobashgr/BC5CDR-chem-WLT-384-BioELECTRA-Pubmed-ENS-20-5 — chemicals
    mobashgr/NCBI-disease-WLT-256-SciBERT-13INS           — diseases
    alvaroalon2/biobert_genetic_ner                       — genes / proteins

Plus, when wired into the pipeline, the LLM extractor adds items with
``source_model="llm_ner:<model_string>"``.

Dependencies:
    pip install transformers torch

When a model fails to load (missing weights, no GPU, etc.), the runner logs
a warning and skips that model — never crashes the pipeline.

Returned shape per mention (matches the LLM extractor's per-mention shape
so the same downstream code handles both):

    {
      "entity":   "BDNF",
      "label":    "Gene",
      "sentence": "BDNF is upregulated in the hippocampus.",
      "start":    0,
      "end":      4,
      "paper_location": null,
      "source_model":   "alvaroalon2/biobert_genetic_ner",
      "source_score":   0.998      # model's own confidence (0–1), if available
    }
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

logger = logging.getLogger("ner_models")


# ---------------------------------------------------------------------------
# Default roster
# ---------------------------------------------------------------------------

# Comprehensive default roster. The runner skips unavailable models
# gracefully (missing weights / no transformers / no GPU), so an over-broad
# default is safe — set `hf_models=` explicitly to constrain.
DEFAULT_HF_MODELS: list[str] = [
    # broad biomedical
    "d4data/biomedical-ner-all",

    # BC5CDR-derived specialists
    "mobashgr/BC5CDR-chem-WLT-384-BioELECTRA-Pubmed-ENS-20-5",
    "mobashgr/NCBI-disease-WLT-256-SciBERT-13INS",

    # BioBERT genetic
    "alvaroalon2/biobert_genetic_ner",

    # BENT family — consistent PubMedBERT base, one head per entity type.
    "pruas/BENT-PubMedBERT-NER-Gene",
    "pruas/BENT-PubMedBERT-NER-Chemical",
    "pruas/BENT-PubMedBERT-NER-Disease",
    "pruas/BENT-PubMedBERT-NER-Anatomical",
    "pruas/BENT-PubMedBERT-NER-Cell-Type",
    "pruas/BENT-PubMedBERT-NER-Cell-Line",
    "pruas/BENT-PubMedBERT-NER-Organism",
    "pruas/BENT-PubMedBERT-NER-Bioprocess",
]


# Domain profiles — narrower rosters that cost less to load / run when you
# know your domain. Pick by passing `profile=` to ``EnsembleConfig``.
NER_MODEL_PROFILES: dict[str, list[str]] = {
    "biomedical_broad": [
        "d4data/biomedical-ner-all",
        "pruas/BENT-PubMedBERT-NER-Disease",
        "pruas/BENT-PubMedBERT-NER-Chemical",
        "pruas/BENT-PubMedBERT-NER-Gene",
        "pruas/BENT-PubMedBERT-NER-Organism",
    ],
    "cns_cells": [
        "pruas/BENT-PubMedBERT-NER-Cell-Type",
        "pruas/BENT-PubMedBERT-NER-Cell-Line",
        "pruas/BENT-PubMedBERT-NER-Anatomical",
        "pruas/BENT-PubMedBERT-NER-Gene",
        "alvaroalon2/biobert_genetic_ner",
        "d4data/biomedical-ner-all",
    ],
    "pharmacology": [
        "mobashgr/BC5CDR-chem-WLT-384-BioELECTRA-Pubmed-ENS-20-5",
        "pruas/BENT-PubMedBERT-NER-Chemical",
        "pruas/BENT-PubMedBERT-NER-Disease",
        "mobashgr/NCBI-disease-WLT-256-SciBERT-13INS",
    ],
    "genetic": [
        "alvaroalon2/biobert_genetic_ner",
        "pruas/BENT-PubMedBERT-NER-Gene",
        "d4data/biomedical-ner-all",
    ],
    "clinical": [
        # general clinical / EHR-style NER
        "Clinical-AI-Apollo/Medical-NER",
        "blaze999/Medical-NER",
        "d4data/biomedical-ner-all",
        "pruas/BENT-PubMedBERT-NER-Disease",
    ],
    "minimal": [
        # smallest sensible set: one broad + one disease + one gene
        "d4data/biomedical-ner-all",
        "pruas/BENT-PubMedBERT-NER-Gene",
        "pruas/BENT-PubMedBERT-NER-Disease",
    ],
    "all": "DEFAULT",   # sentinel — expanded at runtime to DEFAULT_HF_MODELS
}


# Map raw model-emitted labels to the skill's canonical label taxonomy where
# the mapping is unambiguous. Anything not in this map is passed through
# verbatim (the grouped view + judge stage can sort out edge cases).
_LABEL_NORMALIZATION = {
    # d4data emits "Disease_disorder", "Sign_symptom", "Detailed_description"…
    "Disease_disorder":      "Disease",
    "Sign_symptom":          "Phenotype",
    "Therapeutic_procedure": "Method",
    "Diagnostic_procedure":  "Method",
    "Biological_structure":  "Anatomy",
    "Body_system":           "Anatomy",
    "Sex":                   "Other",
    "Age":                   "Measurement",
    "Quantitative_concept":  "Measurement",
    "Biological_attribute":  "Measurement",

    # BC5CDR / BioBERT raw labels (some emit single-class output)
    "Chemical":  "Chemical",
    "Drug":      "Drug",
    "Disease":   "Disease",
    "Gene":      "Gene",
    "Protein":   "Protein",
    "GENE":      "Gene",
    "DRUG":      "Drug",
    "DISEASE":   "Disease",
    "CHEMICAL":  "Chemical",

    # BENT-PubMedBERT family — each single-head model returns a generic
    # "B-<TYPE>" / "I-<TYPE>" or aggregated label. Most are unambiguous.
    "Anatomical":           "Anatomy",
    "Cell-Type":            "CellType",
    "Cell_Type":            "CellType",
    "Cell-Line":            "CellLine",
    "Cell_Line":            "CellLine",
    "Organism":             "Species",
    "Bioprocess":           "Phenomenon",
    "Molecular":            "Protein",  # BENT "Molecular" tags molecular entities (mostly proteins)
}


def _normalize_label(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    return _LABEL_NORMALIZATION.get(raw, raw)


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------

def _sentence_for(text: str, start: int, end: int) -> str:
    """Return the smallest substring of ``text`` containing [start, end] that
    starts after a ``. !? \\n`` boundary and ends at one.

    Lightweight — no spaCy required.
    """
    # walk backwards to the last sentence terminator
    s = start
    while s > 0 and text[s - 1] not in ".!?\n":
        s -= 1
    while s < start and text[s] in " \t\n":
        s += 1
    # walk forwards to the next sentence terminator
    e = end
    n = len(text)
    while e < n and text[e - 1] not in ".!?\n":
        e += 1
    return text[s:e].strip()


# ---------------------------------------------------------------------------
# Model runners (lazy imports — each tolerates missing deps)
# ---------------------------------------------------------------------------

@dataclass
class ModelResult:
    source_model: str
    items: list[dict] = field(default_factory=list)
    skipped_reason: Optional[str] = None


def run_hf_token_classifier(text: str, model_name: str,
                            aggregation: str = "simple",
                            device: int = -1) -> ModelResult:
    """Run a HuggingFace token-classification pipeline (BERT-style NER model)."""
    try:
        from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
    except ImportError as e:
        return ModelResult(source_model=model_name,
                           skipped_reason=f"transformers not installed: {e}")
    try:
        tok = AutoTokenizer.from_pretrained(model_name)
        mdl = AutoModelForTokenClassification.from_pretrained(model_name)
        nlp = pipeline("ner", model=mdl, tokenizer=tok,
                       aggregation_strategy=aggregation, device=device)
    except Exception as e:
        return ModelResult(source_model=model_name,
                           skipped_reason=f"failed to load: {e}")

    items: list[dict] = []
    # Long inputs: chunk to the tokenizer's max length to avoid truncation
    # errors. Each HF call returns spans relative to the chunk; we re-anchor.
    try:
        max_len = tok.model_max_length or 512
    except AttributeError:
        max_len = 512
    # rough char-to-token ratio for biomedical text ≈ 4
    chunk_chars = max(256, (max_len - 32) * 4)

    cursor = 0
    while cursor < len(text):
        # break at a whitespace boundary near chunk_chars to avoid cutting words
        end = min(len(text), cursor + chunk_chars)
        if end < len(text):
            space = text.rfind(" ", cursor, end)
            if space > cursor + chunk_chars // 2:
                end = space
        chunk = text[cursor:end]
        try:
            results = nlp(chunk)
        except Exception as e:
            logger.warning("hf %s failed on chunk: %s", model_name, e)
            cursor = end
            continue
        for r in results or []:
            start = int(r.get("start") or 0) + cursor
            stop = int(r.get("end") or 0) + cursor
            surface = text[start:stop]
            items.append({
                "entity":   surface,
                "label":    _normalize_label(r.get("entity_group") or r.get("entity")),
                "sentence": _sentence_for(text, start, stop),
                "start":    start,
                "end":      stop,
                "paper_location": None,
                "source_model":   model_name,
                "source_score":   float(r.get("score") or 0.0),
            })
        cursor = end
    return ModelResult(source_model=model_name, items=items)


# ---------------------------------------------------------------------------
# Ensemble driver
# ---------------------------------------------------------------------------

@dataclass
class EnsembleConfig:
    """Configure the NER ensemble.

    Pick models EITHER by ``profile`` (one of NER_MODEL_PROFILES) OR by
    ``hf_models`` (explicit list). If both are set, ``hf_models`` wins.
    """
    hf_models: Optional[list[str]] = None
    profile:   Optional[str] = None   # "biomedical_broad" | "cns_cells" | …
    device:    int = -1               # -1 = CPU; >=0 = CUDA device index
    max_workers: int = 4
    min_score:   float = 0.0          # drop HF hits below this score

    def resolved_models(self) -> list[str]:
        if self.hf_models is not None:
            return list(self.hf_models)
        if self.profile:
            picked = NER_MODEL_PROFILES.get(self.profile)
            if picked is None:
                raise ValueError(
                    f"unknown NER profile {self.profile!r}. "
                    f"Available: {sorted(NER_MODEL_PROFILES)}")
            if picked == "DEFAULT":
                return list(DEFAULT_HF_MODELS)
            return list(picked)
        return list(DEFAULT_HF_MODELS)


def run_ensemble(text: str, config: Optional[EnsembleConfig] = None,
                 ) -> tuple[list[dict], list[dict]]:
    """Run every configured model in parallel. Returns (items, per_model_meta).

    items: flat list of mentions across all models, each with source_model
    per_model_meta: [{"source_model", "count", "skipped_reason"}]
    """
    cfg = config or EnsembleConfig()
    models = cfg.resolved_models()
    jobs: list[Callable[[], ModelResult]] = [
        (lambda mid=mid: run_hf_token_classifier(text, mid, device=cfg.device))
        for mid in models
    ]

    items: list[dict] = []
    meta: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        for res in ex.map(lambda f: f(), jobs):
            if res.skipped_reason:
                logger.warning("skipped %s: %s", res.source_model, res.skipped_reason)
                meta.append({"source_model": res.source_model, "count": 0,
                             "skipped_reason": res.skipped_reason})
                continue
            keep = [it for it in res.items if
                    it.get("source_score", 1.0) >= cfg.min_score]
            items.extend(keep)
            meta.append({"source_model": res.source_model,
                         "count": len(keep),
                         "skipped_reason": None})
    return items, meta


# ---------------------------------------------------------------------------
# Merge with LLM extractor output (the prompts/extractor-ner-*.md results)
# ---------------------------------------------------------------------------

def annotate_llm_provenance(llm_entities: Iterable[dict], llm_model: str) -> list[dict]:
    """Tag every LLM-emitted item with source_model so it merges cleanly with
    ensemble output. Call this on the result of pipeline.extract().
    """
    out = []
    for it in llm_entities:
        it = dict(it)
        it.setdefault("source_model", f"llm_ner:{llm_model}")
        out.append(it)
    return out


def merge_ensemble_and_llm(ensemble_items: list[dict],
                           llm_items: list[dict]) -> list[dict]:
    """Concatenate; do NOT deduplicate. Different models surfacing the same
    span is signal, not noise — the grouped view will record both sources
    and the consensus count.
    """
    return list(ensemble_items) + list(llm_items)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = ("BDNF is upregulated in the hippocampus of adult mice. "
            "Patients with Alzheimer's disease show reduced BDNF protein levels. "
            "We administered ketamine at 10 mg/kg.")
    # Smoke test with an empty model list — proves the runner doesn't crash
    # when transformers isn't installed or no models are configured.
    items, meta = run_ensemble(demo, EnsembleConfig(hf_models=[]))
    print("per-model:", meta)
    print(f"\n{len(items)} items")
    print("\nAvailable profiles:", sorted(NER_MODEL_PROFILES))
