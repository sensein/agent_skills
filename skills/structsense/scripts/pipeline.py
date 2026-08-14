"""End-to-end reference pipeline: extract -> align -> judge.

Standalone, model-agnostic. Wires together the other scripts in this folder.

Usage:
    # one paper
    python -m scripts.pipeline --task ner --input paper.txt \
        --extractor openrouter/anthropic/claude-sonnet-4-6 \
        --judge openrouter/openai/gpt-4o-mini \
        --mapper ols \
        --out result.json

    # a corpus: every .txt in a directory, each to <stem>_final.json, then merged
    # into corpus_synthesis.{json,md}. --no-synthesize skips the merge.
    python -m scripts.pipeline --task ner --input ./papers \
        --extractor openrouter/anthropic/claude-sonnet-4-6 \
        --ner-profile cns_cells

Several inputs are processed in turn and one failure does not abort the batch: the
exit code is 2 when some succeeded and 1 when none did, matching abcd_extract. The
corpus roll-up is decided by the input count unless --synthesize/--no-synthesize
says otherwise, and per-paper results stay the authoritative record (SKILL.md
rules 9 and 9b).

This is a *reference* implementation, not a framework. Copy and adapt.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Sibling imports are bare, so `scripts/` itself must be importable. Running the
# documented `python -m scripts.pipeline` puts the *parent* on sys.path, not this
# directory, so every one of these failed with ModuleNotFoundError: no module named
# 'chunking' — the command in the docs did not work. Add this directory explicitly.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from chunking import chunk_by_sentences, reanchor_items, dedupe  # noqa: E402
from json_repair import parse_or_repair
from span_validator import validate_all
from llm_client import call as llm_call
from stats import compute_stats, format_summary
from group_by_entity import attach_grouped_views
from ner_models import (
    run_ensemble, EnsembleConfig, annotate_llm_provenance, merge_ensemble_and_llm,
)
from normalize_result import (
    lift_doc_metadata, tag_missing_source_model,
)

logger = logging.getLogger("pipeline")

# Prompt files — load once from the prompts/ directory.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a system prompt from prompts/<name>.md. Strips the markdown
    scaffolding and returns the contents of the first fenced ``` block under
    a `## System` heading.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    text = path.read_text()
    marker = "## System"
    if marker not in text:
        raise RuntimeError(f"{path}: missing '## System' heading")
    after = text.split(marker, 1)[1]
    if "```" not in after:
        raise RuntimeError(f"{path}: no fenced block under ## System")
    body = after.split("```", 2)[1]
    # Strip a leading language hint if present.
    if body.startswith("\n"):
        body = body[1:]
    return body.strip()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract(text: str, *, model: str, task: str,
            metadata: Optional[dict] = None,
            chunk_size: int = 2000, max_workers: int = 8) -> dict:
    """Chunk -> extract per chunk in parallel -> reanchor -> merge -> dedup."""
    system = _load_prompt(f"extractor-{task}")
    chunks = chunk_by_sentences(text, max_chars=chunk_size)
    logger.info("extract: %d chunks", len(chunks))

    def one(c):
        user = (
            f"INPUT TEXT:\n<<<\n{c['text']}\n>>>\n\n"
            f"METADATA:\n{json.dumps(metadata or {}, indent=2)}\n"
        )
        raw = llm_call(model=model, system=system, user=user,
                       json_mode=True, temperature=0)
        parsed = parse_or_repair(raw) or {}
        return c["start"], parsed

    results: list[tuple[int, dict]] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(one, chunks):
            results.append(r)

    entities, key_terms, resources = [], [], []
    for chunk_start, r in results:
        if "entities" in r:
            entities.extend(reanchor_items(r["entities"], chunk_start))
        if "key_terms" in r:
            key_terms.extend(reanchor_items(r["key_terms"], chunk_start))
        if "extracted_resources" in r:
            for k, lst in (r["extracted_resources"] or {}).items():
                resources.extend(lst or [])

    out: dict[str, Any] = {}
    if entities or task == "ner":
        valid, dropped = validate_all(text, entities)
        if dropped:
            logger.warning("dropped %d entities with invalid spans", len(dropped))
        out["entities"] = dedupe(valid, key_fields=("entity", "start", "end"))
    if key_terms or task == "ner":
        valid, dropped = validate_all(text, key_terms)
        out["key_terms"] = dedupe(valid, key_fields=("term", "start", "end"))
    if resources:
        out["extracted_resources"] = {"1": resources}
    out["task_type"] = task
    return out


# ---------------------------------------------------------------------------
# Alignment via direct mapping tool (no LLM)
# ---------------------------------------------------------------------------

def align_direct(extraction: dict, *,
                 mapper_backend: Optional[str] = None,
                 mapper=None,
                 ontologies_for_label: Optional[Callable[[str], list[str]]] = None
                 ) -> dict:
    """Walk extraction items, batch-map their surface forms, merge the mappings
    back in. No LLM. Marks every item with alignment_method=direct_tool_call.

    Either pass ``mapper`` (a pre-built client from build_mapper_with_cascade)
    or ``mapper_backend`` (a string name) — the latter is for tests/standalone
    use; the production path uses the cascade builder.
    """
    if mapper is None:
        if mapper_backend is None:
            raise ValueError("align_direct: pass either mapper or mapper_backend")
        mapper = _make_mapper(mapper_backend)

    def annotate(items: list[dict], surface_key: str):
        if not items:
            return
        terms = [it.get(surface_key) for it in items]
        ontologies = (ontologies_for_label(items[0].get("label")) if ontologies_for_label
                      else None)
        mappings = mapper.map_batch(terms, ontologies=ontologies, max_results=1)
        for it, m in zip(items, mappings):
            it["ontology_id"] = m.get("ontology_id")
            it["ontology_label"] = m.get("ontology_label")
            it["ontology"] = m.get("ontology")
            it["concept_mapping_provenance"] = m.get("concept_mapping_provenance",
                                                     "unmapped")
            it["alignment_method"] = "direct_tool_call"

    if "entities" in extraction:
        annotate(extraction["entities"], "entity")
    if "key_terms" in extraction:
        annotate(extraction["key_terms"], "term")
    return extraction


def _make_mapper(backend: str, **kwargs):
    backend = (backend or "").lower()
    if backend == "ols":
        from ols_map import OlsMapper
        return OlsMapper()
    if backend == "bioportal":
        from bioportal_map import BioPortalMapper
        return BioPortalMapper()
    if backend in ("local", "local_hybrid"):
        from local_hybrid_map import LocalHybridMapper
        return LocalHybridMapper(**kwargs)
    raise ValueError(f"unknown mapper backend: {backend!r}")


def build_mapper_with_cascade(
    preferred: str = "local",
    *,
    local_url: str = "http://localhost:8000",
    ask_user: Optional[Callable[[str], Optional[str]]] = None,
    allow_ols_fallback: bool = False,
) -> tuple[object, dict]:
    """Build a concept-mapping client with fallbacks.

    **Policy: concept mapping is MANDATORY.** This function never silently
    returns "no mapper". It either returns a working tool-backed mapper, or
    raises RuntimeError so the caller can surface the problem to the user.

    Cascade (default ``preferred='local'``):
      1. Try local hybrid service at ``local_url`` (default http://localhost:8000).
         Health-check via GET /health then /docs.
      2. On failure, try BioPortal (requires BIOPORTAL_API_KEY).
      3. On further failure, call ``ask_user`` (if provided) for an
         alternative local URL — deployments use non-default ports and
         reverse-proxied paths. If ``ask_user`` returns a non-empty URL,
         retry the local backend with that URL.
      4. Final fallback: raise RuntimeError. The caller MUST surface this
         to the user, not silently skip alignment with `llm_knowledge`.

    OLS is no longer in the default cascade — it doesn't cover genes
    (HGNC), and the policy is to prefer the local hybrid + BioPortal for
    completeness. Pass ``allow_ols_fallback=True`` to enable it as a last
    resort (only useful for anatomy / cell / disease / chemical extraction).

    ``ask_user(prompt) -> Optional[str]`` is the integration point for
    interactive callers (CLI prompts, or a Claude Code skill that asks
    the user via natural language). Pass ``ask_user=None`` for fully
    automatic runs that fail fast.

    Returns:
        (mapper, meta) where meta is suitable for `alignment_meta` in
        stats.compute_stats(). Keys:
          mapper_used:     "local_hybrid" | "bioportal"
          mapper_url:      str
          fallback_triggered: bool
          cascade_history: list[str]   # each backend attempted
          user_provided_url: bool
    """
    history: list[str] = []
    user_provided_url = False

    def _try_local(url: str):
        from local_hybrid_map import LocalHybridMapper
        m = LocalHybridMapper(base_url=url)
        if not m.health():
            raise RuntimeError(f"local_hybrid health check failed at {url}")
        return m

    def _try_bioportal():
        if not os.environ.get("BIOPORTAL_API_KEY"):
            raise RuntimeError("BIOPORTAL_API_KEY not set")
        from bioportal_map import BioPortalMapper
        return BioPortalMapper()

    # 1) preferred local (or anything that's the preferred backend)
    if preferred in ("local", "local_hybrid"):
        history.append(f"local_hybrid@{local_url}")
        try:
            m = _try_local(local_url)
            return m, {"mapper_used": "local_hybrid",
                       "mapper_url": local_url,
                       "fallback_triggered": False,
                       "cascade_history": history,
                       "user_provided_url": user_provided_url}
        except Exception as e:
            logger.warning("local_hybrid unavailable at %s: %s", local_url, e)
    elif preferred == "bioportal":
        history.append("bioportal")
        try:
            return _try_bioportal(), {"mapper_used": "bioportal",
                                       "mapper_url": "https://data.bioontology.org",
                                       "fallback_triggered": False,
                                       "cascade_history": history,
                                       "user_provided_url": False}
        except Exception as e:
            logger.warning("bioportal unavailable: %s", e)
    elif preferred == "ols":
        history.append("ols")
        from ols_map import OlsMapper
        return OlsMapper(), {"mapper_used": "ols",
                              "mapper_url": "https://www.ebi.ac.uk/ols4/api",
                              "fallback_triggered": False,
                              "cascade_history": history,
                              "user_provided_url": False}
    else:
        raise ValueError(f"unknown preferred backend {preferred!r}")

    # 2) BioPortal fallback (only reached if preferred was 'local' and failed)
    history.append("bioportal")
    try:
        m = _try_bioportal()
        return m, {"mapper_used": "bioportal",
                   "mapper_url": "https://data.bioontology.org",
                   "fallback_triggered": True,
                   "cascade_history": history,
                   "user_provided_url": False}
    except Exception as e:
        logger.warning("bioportal fallback unavailable: %s", e)

    # 3) Ask the user for an alternative URL
    if ask_user is not None:
        prompt = (
            f"Concept-mapping cascade failed.\n"
            f"  tried local_hybrid at {local_url}: not reachable\n"
            f"  tried BioPortal: not configured or unreachable\n"
            f"The local_hybrid service URL may differ in your deployment. "
            f"Enter an alternate URL (e.g. http://localhost:9000) or press "
            f"Enter to skip alignment:"
        )
        alt_url = ask_user(prompt)
        if alt_url and alt_url.strip():
            alt_url = alt_url.strip().rstrip("/")
            history.append(f"local_hybrid@{alt_url} (user-provided)")
            try:
                m = _try_local(alt_url)
                return m, {"mapper_used": "local_hybrid",
                           "mapper_url": alt_url,
                           "fallback_triggered": True,
                           "cascade_history": history,
                           "user_provided_url": True}
            except Exception as e:
                logger.warning("user-provided local_hybrid at %s also failed: %s",
                               alt_url, e)
                history.append("user_url_failed")

    # 4) Optional OLS fallback (only if caller explicitly allowed it).
    #    OLS doesn't cover HGNC genes — only useful for anatomy/cell/disease/chemical.
    if allow_ols_fallback:
        history.append("ols")
        try:
            from ols_map import OlsMapper
            return OlsMapper(), {"mapper_used": "ols",
                                  "mapper_url": "https://www.ebi.ac.uk/ols4/api",
                                  "fallback_triggered": True,
                                  "cascade_history": history,
                                  "user_provided_url": False,
                                  "warning": "OLS fallback active — gene mappings "
                                             "(HGNC) will be missing."}
        except Exception as e:
            logger.warning("OLS fallback failed: %s", e)
            history.append("ols_failed")

    # Cascade exhausted. We do NOT silently skip — the user policy is that
    # concept mapping is mandatory and must come from a tool. The caller
    # should surface this and let the user either (a) start the local
    # service, (b) set BIOPORTAL_API_KEY, or (c) explicitly opt out via
    # mapper_backend=None.
    raise RuntimeError(
        f"concept-mapping cascade exhausted. tried: {history}. "
        f"This skill enforces tool-backed mappings only (no LLM-knowledge "
        f"fallbacks). Either: (a) start the local hybrid mapper at "
        f"{local_url}, (b) export BIOPORTAL_API_KEY, (c) explicitly opt out "
        f"of alignment by passing mapper_backend=None to run() — items will "
        f"be marked concept_mapping_provenance='skipped'."
    )


def _stdin_ask(prompt: str) -> Optional[str]:
    """Default ask_user implementation: read from stdin if attached."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print(prompt, file=sys.stderr, flush=True)
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        return None


# ---------------------------------------------------------------------------
# Judge (batched direct API)
# ---------------------------------------------------------------------------

def judge(aligned: dict, source_text: str, *, model: str,
          max_workers: int = 8, auto_approve: bool = False) -> dict:
    if auto_approve:
        for key in ("entities", "key_terms"):
            for it in aligned.get(key, []) or []:
                it["judge_score"] = 1.0
                it["remarks"] = "auto-approved"
                it["judge_method"] = "auto_approved"
        return aligned

    system = _load_prompt("judge")

    def one(item: dict) -> dict:
        user = (
            f"ALIGNED ITEM TO JUDGE:\n{json.dumps(item, indent=2)}\n\n"
            f"SOURCE CONTEXT:\n{item.get('sentence') or source_text[:2000]}\n"
        )
        raw = llm_call(model=model, system=system, user=user,
                       json_mode=True, temperature=0)
        parsed = parse_or_repair(raw) or {}
        item = dict(item)
        item["judge_score"] = parsed.get("judge_score")
        item["remarks"] = parsed.get("remarks")
        item["judge_method"] = "llm"
        return item

    out = dict(aligned)
    for key in ("entities", "key_terms"):
        items = out.get(key) or []
        if not items:
            continue
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            out[key] = list(ex.map(one, items))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(text: str, *, task: str, extractor_model: str,
        mapper_backend: Optional[str], judge_model: Optional[str],
        chunk_size: int = 2000, max_workers: int = 8,
        skip_judge: bool = False,
        local_mapping_url: str = "http://localhost:8000",
        ask_user: Optional[Callable[[str], Optional[str]]] = None,
        input_path: Optional[str] = None,
        ner_ensemble_profile: Optional[str] = None,
        ner_ensemble_models: Optional[list[str]] = None,
        ner_ensemble_device: int = -1,
        allow_ols_fallback: bool = False) -> dict:
    """Full pipeline: ensemble NER + LLM extract -> align -> judge.

    Set ``ner_ensemble_profile`` (e.g. ``"biomedical_broad"`` or ``"cns_cells"``)
    or pass an explicit ``ner_ensemble_models`` list to run HuggingFace NER
    models alongside the LLM extractor. Each mention carries a ``source_model``
    field; the grouped view records which models contributed to each entity.

    Always embeds a ``stats`` block in the result. See scripts/stats.py
    for the shape.
    """
    started = time.monotonic()
    timings: dict[str, float] = {}
    alignment_meta: dict = {}
    judge_meta: dict = {}
    ensemble_meta: list[dict] = []

    # --- 0. (optional) HuggingFace NER ensemble — runs in parallel with LLM ---
    ensemble_items: list[dict] = []
    if (ner_ensemble_profile or ner_ensemble_models) and task == "ner":
        t0 = time.monotonic()
        cfg = EnsembleConfig(
            hf_models=ner_ensemble_models,
            profile=ner_ensemble_profile,
            device=ner_ensemble_device,
            max_workers=max_workers,
        )
        ensemble_items, ensemble_meta = run_ensemble(text, cfg)
        timings["ensemble_ner"] = round(time.monotonic() - t0, 2)
        logger.info("ensemble produced %d mentions across %d model(s)",
                    len(ensemble_items),
                    sum(1 for m in ensemble_meta if not m.get("skipped_reason")))

    # --- 1. LLM extraction ---
    t0 = time.monotonic()
    extraction = extract(text, model=extractor_model, task=task,
                         chunk_size=chunk_size, max_workers=max_workers)
    timings["extraction"] = round(time.monotonic() - t0, 2)

    # --- 1b. merge LLM + ensemble (NER only) ---
    if ensemble_items and "entities" in extraction:
        extraction["entities"] = annotate_llm_provenance(
            extraction["entities"], llm_model=extractor_model)
        extraction["entities"] = merge_ensemble_and_llm(
            ensemble_items, extraction["entities"])
    elif "entities" in extraction:
        # No ensemble: still tag LLM provenance for consistency.
        extraction["entities"] = annotate_llm_provenance(
            extraction["entities"], llm_model=extractor_model)

    # --- 2. alignment with mapper cascade ---
    if mapper_backend:
        t0 = time.monotonic()
        try:
            mapper, alignment_meta = build_mapper_with_cascade(
                preferred=mapper_backend,
                local_url=local_mapping_url,
                ask_user=ask_user,
                allow_ols_fallback=allow_ols_fallback,
            )
            aligned = align_direct(extraction, mapper=mapper)
        except RuntimeError as e:
            logger.error("alignment cascade exhausted: %s — skipping alignment", e)
            aligned = extraction
            for key in ("entities", "key_terms"):
                for it in aligned.get(key, []) or []:
                    it.setdefault("concept_mapping_provenance", "skipped")
                    it.setdefault("alignment_method", "skipped")
            alignment_meta = {"mapper_used": None,
                              "mapper_url": None,
                              "fallback_triggered": True,
                              "cascade_history": ["all_failed"],
                              "user_provided_url": False}
        timings["alignment"] = round(time.monotonic() - t0, 2)
    else:
        aligned = extraction
        for key in ("entities", "key_terms"):
            for it in aligned.get(key, []) or []:
                it.setdefault("concept_mapping_provenance", "skipped")
                it.setdefault("alignment_method", "skipped")
        alignment_meta = {"mapper_used": None, "mapper_url": None,
                          "fallback_triggered": False, "cascade_history": [],
                          "user_provided_url": False}
        timings["alignment"] = 0.0

    # --- 3. judge ---
    t0 = time.monotonic()
    if judge_model and not skip_judge:
        judged = judge(aligned, text, model=judge_model, max_workers=max_workers)
        judge_meta = {"method": "llm", "model": judge_model, "max_workers": max_workers}
    else:
        judged = judge(aligned, text, model="", auto_approve=True)
        judge_meta = {"method": "auto_approved", "model": None}
    timings["judge"] = round(time.monotonic() - t0, 2)

    timings["total"] = round(time.monotonic() - started, 2)
    judged["elapsed_time"] = timings["total"]

    # --- 3b. NORMALIZE TO CANONICAL SHAPE (safety net) ---
    # Even when the LLM ignores the new prompt and emits per-entity
    # paper_title / doi, this lifts them to top-level source_metadata,
    # strips them off entities, and tags missing source_model so the
    # downstream grouped view + stats are consistent regardless of what
    # the LLM produced.
    lift_doc_metadata(judged, input_path=input_path)
    tag_missing_source_model(judged, extractor_model)

    # --- 4. attach grouped-by-entity view (entities_grouped + key_terms_grouped) ---
    # Raw mentions stay as the authoritative record; the grouped view exists
    # so downstream consumers can navigate by canonical entity ("everything
    # about BDNF in this paper") and so multi-sentence context naturally
    # emerges from merging sentences across all mentions.
    attach_grouped_views(judged)

    # --- 5. stats (must run AFTER grouping so it can include unique counts) ---
    if ensemble_meta:
        judged["ensemble_models"] = ensemble_meta
    judged["stats"] = compute_stats(
        judged,
        timings=timings,
        input_meta={"char_count": len(text),
                    "chunk_count": _chunk_count(text, chunk_size),
                    "chunk_size_chars": chunk_size,
                    "input_path": input_path},
        alignment_meta=alignment_meta,
        judge_meta=judge_meta,
    )
    return judged


def _chunk_count(text: str, chunk_size: int) -> int:
    if chunk_size <= 0 or len(text) <= chunk_size:
        return 1
    return len(chunk_by_sentences(text, max_chars=chunk_size))


def default_output_path(input_path: Optional[str], explicit_out: Optional[str]) -> str:
    """Pick the output filename.

    - If ``explicit_out`` is given, use it verbatim.
    - Else if ``input_path`` ends in a known text extension, replace the
      extension with ``_final.json`` (e.g. ``paper.txt`` -> ``paper_final.json``,
      ``paper.pdf`` -> ``paper_final.json``).
    - Else fall back to ``result_final.json``.
    """
    if explicit_out:
        return explicit_out
    if input_path:
        p = Path(input_path)
        return str(p.with_name(p.stem + "_final.json"))
    return "result_final.json"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["ner", "resource", "structured"], default="ner")
    ap.add_argument("--input", required=True, action="append",
                    help="path to a .txt input, or a directory of them. Repeatable. "
                         "More than one input runs each in turn and then merges the "
                         "per-paper results into one corpus roll-up (rule 9b).")
    ap.add_argument("--input-glob", default="*.txt",
                    help="pattern used when --input is a directory (default *.txt)")
    ap.add_argument("--synthesize", dest="synthesize", action="store_true",
                    default=None,
                    help="force the corpus roll-up even for a single input")
    ap.add_argument("--no-synthesize", dest="synthesize", action="store_false",
                    help="skip the corpus roll-up even with several inputs")
    ap.add_argument("--corpus-out", default=None,
                    help="output stem for the roll-up (default: <out dir>/corpus_synthesis)")
    ap.add_argument("--extractor", required=True, help="extractor model string")
    ap.add_argument("--judge", default=None, help="judge model string (omit to auto-approve)")
    ap.add_argument("--mapper", choices=["ols", "bioportal", "local", "none"], default="local",
                    help="preferred mapping backend. 'local' is the default cascade: "
                         "local hybrid (http://localhost:8000) → BioPortal → interactive "
                         "prompt for alternative URL → HARD STOP. OLS is NOT in the "
                         "default cascade (no gene coverage); pass --mapper ols to use "
                         "it explicitly, or --allow-ols-fallback to add it as a last "
                         "resort. 'none' explicitly opts out of mapping (items get "
                         "concept_mapping_provenance='skipped').")
    ap.add_argument("--allow-ols-fallback", action="store_true",
                    help="Allow OLS as a last-resort fallback when local + BioPortal "
                         "both fail. Off by default because OLS lacks gene coverage.")
    ap.add_argument("--mapper-url", default="http://localhost:8000",
                    help="URL for the local hybrid mapping service. Overrides "
                         "LOCAL_CONCEPT_MAPPING_URL env var. /docs is a good "
                         "endpoint to verify the service is up.")
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--out", default=None,
                    help="Output JSON path. Defaults to <input-stem>_final.json.")
    ap.add_argument("--non-interactive", action="store_true",
                    help="Do not prompt for an alternative mapper URL when "
                         "the cascade fails. Fail fast instead.")
    ap.add_argument("--ner-profile", default=None,
                    choices=["biomedical_broad", "cns_cells", "pharmacology",
                             "genetic", "clinical", "minimal", "all"],
                    help="Enable the HuggingFace NER ensemble alongside the LLM "
                         "extractor. Picks a domain-appropriate set of models. "
                         "Each mention gets a `source_model` field; the grouped "
                         "view records which models surfaced each entity. "
                         "Requires `pip install transformers torch`.")
    ap.add_argument("--ner-models", default=None,
                    help="Comma-separated list of HF model IDs for an explicit "
                         "ensemble. Overrides --ner-profile.")
    ap.add_argument("--ner-device", type=int, default=-1,
                    help="CUDA device index for HF NER models (-1 = CPU). "
                         "Default -1.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mapper = None if args.mapper == "none" else args.mapper

    ner_models = (
        [m.strip() for m in args.ner_models.split(",") if m.strip()]
        if args.ner_models else None
    )

    # Resolve inputs: files as given, directories expanded by --input-glob.
    inputs: list[Path] = []
    for raw in args.input:
        p = Path(raw)
        if p.is_dir():
            found = sorted(f for f in p.glob(args.input_glob) if f.is_file())
            if not found:
                raise SystemExit(f"{p}: nothing matching {args.input_glob!r}")
            inputs.extend(found)
        else:
            inputs.append(p)
    if len(inputs) > 1 and args.out:
        # --out names one file; with several inputs each would overwrite the last and
        # only the final paper would survive, silently.
        raise SystemExit("--out names a single file; with several inputs let each "
                         "result use the <stem>_final.json convention and set "
                         "--corpus-out for the roll-up")

    written: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for i, in_path in enumerate(inputs, 1):
        if len(inputs) > 1:
            print(f"[{i}/{len(inputs)}] {in_path}", file=sys.stderr)
        try:
            result = run(
                in_path.read_text(), task=args.task,
                extractor_model=args.extractor,
                mapper_backend=mapper,
                judge_model=args.judge,
                chunk_size=args.chunk_size,
                max_workers=args.max_workers,
                skip_judge=args.judge is None,
                local_mapping_url=args.mapper_url,
                ask_user=None if args.non_interactive else _stdin_ask,
                input_path=str(in_path),
                ner_ensemble_profile=args.ner_profile,
                ner_ensemble_models=ner_models,
                ner_ensemble_device=args.ner_device,
                allow_ols_fallback=args.allow_ols_fallback,
            )
        except Exception as exc:
            # One bad paper must not lose the rest of a long batch. Mirrors
            # abcd_extract's bulk behaviour, including the partial-failure exit code.
            if len(inputs) == 1:
                raise
            failed.append((in_path, f"{type(exc).__name__}: {exc}"))
            print(f"  FAILED {in_path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        out_path = Path(default_output_path(str(in_path), args.out))
        out_path.write_text(json.dumps(result, indent=2, default=str))
        written.append(out_path)
        print(format_summary(result["stats"]), file=sys.stderr)
        print(f"wrote {out_path}", file=sys.stderr)

    # Corpus roll-up. Auto-detected from the input count, the same way
    # abcd_extract decides on its cross-paper synthesis; --synthesize /
    # --no-synthesize override. Per-paper files stay the authoritative record.
    do_synth = args.synthesize if args.synthesize is not None else len(written) > 1
    if do_synth and written:
        from merge_corpus import build_corpus, render_markdown

        stem = Path(args.corpus_out) if args.corpus_out else \
            written[0].parent / "corpus_synthesis"
        stem.parent.mkdir(parents=True, exist_ok=True)
        corpus = build_corpus(written, include_mentions=False, with_index=True)
        stem.with_suffix(".json").write_text(
            json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
        stem.with_suffix(".md").write_text(render_markdown(corpus, top_n=50) + "\n")
        print(f"wrote {stem.with_suffix('.json')}", file=sys.stderr)
        print(f"wrote {stem.with_suffix('.md')}", file=sys.stderr)
    elif len(written) > 1:
        print("corpus roll-up skipped (--no-synthesize); per-paper files only",
              file=sys.stderr)

    if failed:
        print(f"{len(failed)} of {len(inputs)} input(s) failed", file=sys.stderr)
        # 2 = partial success, 1 = nothing succeeded (abcd_extract's convention).
        return 2 if written else 1
    return 0


if __name__ == "__main__":
    # _main returns 2 on partial batch failure and 1 when nothing succeeded, so the
    # exit code has to be propagated — a bare _main() would always exit 0 and a CI
    # step wrapping a bulk run could not tell a clean run from a half-failed one.
    raise SystemExit(_main())
