"""ABCD/HBCD extraction driver — one PDF or a whole directory.

Pipeline per paper:

    load text (input_loader: GROBID -> PyMuPDF -> pdfminer)
      -> chunk to the model's context window (chunking + model_context)
      -> LLM extract with prompts/extractor-abcd.md  (variables/constructs/models/findings)
      -> merge chunk payloads
      -> STRICT VERIFY (abcd_verify): quote must be findable in THIS paper
      -> dictionary-gate variables (abcd_dictionary) + construct-map (cognitive_atlas)
      -> write <stem>_abcd.{json,md,ttl}

What this deliberately does NOT do: infer variables a paper "probably" used, or
enumerate the data dictionary. The paper is the only source of what was used; the
dictionary only decides whether a mentioned name is real, and which release(s)
contain it.

ONE ARGUMENT, no mode flags. The input is auto-detected (see `abcd_inputs`):

    python -m scripts.abcd_extract paper.pdf              --llm-model MODEL
    python -m scripts.abcd_extract ./papers               --llm-model MODEL   # directory
    python -m scripts.abcd_extract paper_titles_dois.csv  --llm-model MODEL   # DOIs -> fetch OA PDFs
    python -m scripts.abcd_extract 10.1038/s41586-024-00001-2 --llm-model MODEL

More than one paper implies a cross-paper synthesis; a single paper does not need
one. Suppress with --no-synthesize, force with --synthesize.

    # re-verify an existing extraction against the paper (no LLM calls)
    python -m scripts.abcd_extract paper.pdf --reverify paper_abcd.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts import abcd_export, abcd_inputs
from scripts.abcd_dictionary import Dictionary, DictionaryError
from scripts.abcd_verify import verify_payload
from scripts.cognitive_atlas import CognitiveAtlas, CognitiveAtlasError

SKILL_VERSION = "0.5.0"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extractor-abcd.md"
PDF_SUFFIXES = (".pdf", ".txt", ".md")

SECTIONS = ("variables", "constructs", "models", "findings")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# text + LLM
# --------------------------------------------------------------------------- #

def load_text(path: Path, *, grobid_url: Optional[str] = None) -> Tuple[str, str]:
    """Return (text, extractor_name)."""
    from scripts.input_loader import process_file

    result = process_file(path, grobid_url=grobid_url)
    if isinstance(result, tuple):
        text, meta = result[0], (result[1] if len(result) > 1 else {})
        extractor = (meta or {}).get("extractor", "unknown") if isinstance(meta, dict) else "unknown"
    else:
        text, extractor = result, "unknown"
    return text, extractor


def _chunks(text: str, model: str) -> List[str]:
    try:
        from scripts.chunking import chunk_text
        from scripts.model_context import chunk_chars_for

        size = chunk_chars_for(model)
    except Exception:
        size = 20000
        try:
            from scripts.chunking import chunk_text
        except Exception:
            return [text]
    try:
        return list(chunk_text(text, size))
    except Exception:
        return [text[i: i + size] for i in range(0, len(text), size)] or [text]


def _call_llm(prompt: str, text: str, model: str) -> dict:
    from scripts.json_repair import loads_repaired
    from scripts.llm_client import complete

    raw = complete(
        model=model,
        system=(prompt + "\n\nOutput strict JSON only. No prose. No markdown fences."),
        user=text,
        temperature=0,
    )
    return loads_repaired(raw) if isinstance(raw, str) else (raw or {})


def _merge(payloads: List[dict]) -> dict:
    out: Dict[str, Any] = {k: [] for k in SECTIONS}
    meta: Dict[str, Any] = {}
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for k in SECTIONS:
            items = p.get(k)
            if isinstance(items, list):
                out[k].extend(i for i in items if isinstance(i, dict))
        for mk in ("study", "data_release", "paper_title", "doi", "sample_size",
                   "design"):
            if p.get(mk) and mk not in meta:
                meta[mk] = p[mk]
        sm = p.get("source_metadata")
        if isinstance(sm, dict):
            for mk, mv in sm.items():
                meta.setdefault(mk, mv)
    out["_meta"] = meta
    return out


# --------------------------------------------------------------------------- #
# one paper
# --------------------------------------------------------------------------- #

def extract_paper(path: Path, *, llm_model: str, dictionary: Optional[Dictionary],
                  atlas: Optional[CognitiveAtlas], grobid_url: Optional[str] = None,
                  payload_override: Optional[dict] = None) -> dict:
    """Extract + verify one paper. `payload_override` skips the LLM (re-verify)."""
    started = time.time()
    text, extractor = load_text(path, grobid_url=grobid_url)
    if not text.strip():
        raise RuntimeError(f"{path} produced no text — is it a scanned PDF? "
                           "Run OCR first, or pass a .txt sidecar.")

    if payload_override is not None:
        merged = {k: payload_override.get(k) or [] for k in SECTIONS}
        merged["_meta"] = payload_override.get("source_metadata") or {}
        chunks_used = payload_override.get("provenance", {}).get("chunks")
    else:
        prompt = PROMPT_PATH.read_text()
        parts = _chunks(text, llm_model)
        payloads = []
        for i, part in enumerate(parts, 1):
            print(f"  [{path.name}] chunk {i}/{len(parts)} ({len(part)} chars)",
                  file=sys.stderr)
            payloads.append(_call_llm(prompt, part, llm_model))
        merged = _merge(payloads)
        chunks_used = len(parts)

    meta = merged.pop("_meta", {}) or {}
    verified = verify_payload(merged, text, dictionary=dictionary, atlas=atlas)

    paper_id = meta.get("doi") or path.stem
    doc = {
        "paper_id": str(paper_id),
        "source_metadata": {
            "paper_title": meta.get("paper_title"),
            "doi": meta.get("doi"),
            "source_path": str(path),
            "study": meta.get("study"),
            "data_release": meta.get("data_release"),
            "sample_size": meta.get("sample_size"),
            "design": meta.get("design"),
        },
        **verified,
        "provenance": {
            "run_at": _now(),
            "skill_version": SKILL_VERSION,
            "llm_model": llm_model if payload_override is None else "(re-verified, no LLM)",
            "text_extractor": extractor,
            "text_chars": len(text),
            "chunks": chunks_used,
            "elapsed_sec": round(time.time() - started, 2),
            "dictionaries": dictionary.provenance if dictionary else [],
            "construct_vocabularies": atlas.provenance if atlas else [],
            "verification_policy": {
                "quote_must_appear_in_paper": True,
                "variable_name_must_appear_in_quote": True,
                "construct_id_tool_only": True,
                "variable_must_resolve_in_dictionary_to_be_canonical": True,
            },
        },
    }
    return doc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _resolve_inputs(target: str, *, download_dir: Optional[Path],
                    email: Optional[str], limit: Optional[int]):
    """Auto-detect the input and return (papers, summary). No --bulk needed."""
    papers, summary = abcd_inputs.resolve(target, download_dir=download_dir,
                                          email=email, limit=limit)
    print(f"input {summary['input']!r} detected as {summary['detected_as']}: "
          f"{summary['papers_resolved']}/{summary['papers_total']} papers available",
          file=sys.stderr)
    if summary["papers_unresolved"]:
        print(f"  {summary['papers_unresolved']} could not be resolved "
              f"(first few: "
              + "; ".join(f"{u['doi'] or u['title']}: {u['reason']}"
                          for u in summary["unresolved"][:3]) + ")",
              file=sys.stderr)
    if not papers:
        raise SystemExit("no papers to process")
    return papers, summary


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("input", help="a PDF/TXT, a directory, a CSV/TSV/XLSX of DOIs, "
                                  "a DOI list, or a single DOI — auto-detected")
    ap.add_argument("--llm-model", default=os.getenv("STRUCTSENSE_LLM_MODEL", ""),
                    help="e.g. openai/gpt-4o-mini, anthropic/claude-sonnet-5, ollama/llama3")
    ap.add_argument("--study", default="abcd", choices=["abcd", "hbcd"])
    ap.add_argument("--dd-release", action="append", default=None,
                    help="restrict dictionary snapshots to these releases (repeatable)")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--formats", default="json,md,ttl")
    ap.add_argument("--grobid-url", default=os.getenv("GROBID_URL"))
    ap.add_argument("--reverify", type=Path, default=None,
                    help="re-verify this existing extraction instead of calling an LLM")
    ap.add_argument("--synthesize", dest="synthesize", action="store_true",
                    default=None,
                    help="write a cross-paper synthesis (default: whenever there is "
                         "more than one paper)")
    ap.add_argument("--no-synthesize", dest="synthesize", action="store_false",
                    help="skip the cross-paper synthesis")
    ap.add_argument("--download-dir", type=Path,
                    help="where fetched open-access PDFs go (default <input>/abcd_pdfs)")
    ap.add_argument("--email", help="for Unpaywall when fetching by DOI")
    ap.add_argument("--limit", type=int, help="process at most N papers")
    ap.add_argument("--allow-no-dictionary", action="store_true",
                    help="proceed with variables marked no_dictionary_loaded")
    ap.add_argument("--offline-atlas", action="store_true",
                    help="use only the cached Cognitive Atlas vocabulary")
    a = ap.parse_args(argv)

    formats = [f.strip() for f in a.formats.split(",") if f.strip()]

    # Dictionary — required unless explicitly waived, because without it no
    # variable claim can be verified (only reported).
    dictionary: Optional[Dictionary] = None
    try:
        dictionary = Dictionary.load(study=a.study, releases=a.dd_release)
        loaded = ", ".join(
            "{} {}".format(s["study"], s["release"]) for s in dictionary.snapshots
        )
        print(f"dictionary: {loaded}", file=sys.stderr)
    except DictionaryError as exc:
        if not a.allow_no_dictionary:
            print(f"error: {exc}\n"
                  "Variables cannot be verified without a dictionary. Build one:\n"
                  "  python -m scripts.abcd_dictionary build --study abcd --release latest\n"
                  "or re-run with --allow-no-dictionary to accept unverified variables.",
                  file=sys.stderr)
            return 1
        print(f"warning: {exc} — variables will be marked no_dictionary_loaded",
              file=sys.stderr)

    atlas: Optional[CognitiveAtlas] = None
    try:
        atlas = CognitiveAtlas(offline=a.offline_atlas)
    except CognitiveAtlasError as exc:
        print(f"warning: {exc} — constructs will be reported unmapped", file=sys.stderr)

    if a.reverify is None and not a.llm_model:
        print("error: --llm-model is required (or use --reverify to skip the LLM)",
              file=sys.stderr)
        return 1

    papers, input_summary = _resolve_inputs(
        a.input, download_dir=a.download_dir, email=a.email, limit=a.limit)
    inputs = [p.path for p in papers if p.path]
    fetch_prov = {str(p.path): p.provenance for p in papers if p.origin != "local"}
    in_path = Path(a.input).expanduser()
    out_dir = a.out_dir or (in_path if in_path.is_dir()
                            else in_path.parent if in_path.exists() else Path.cwd())
    override = json.loads(a.reverify.read_text()) if a.reverify else None
    # More than one paper implies a synthesis unless told otherwise.
    do_synth = a.synthesize if a.synthesize is not None else len(inputs) > 1

    written_docs: List[dict] = []
    failures: List[Tuple[Path, str]] = []
    for path in inputs:
        try:
            doc = extract_paper(path, llm_model=a.llm_model, dictionary=dictionary,
                                atlas=atlas, grobid_url=a.grobid_url,
                                payload_override=override)
        except Exception as exc:            # one bad paper must not stop a corpus
            failures.append((path, str(exc)))
            print(f"FAILED {path.name}: {exc}", file=sys.stderr)
            if len(inputs) == 1:
                return 1
            continue
        if str(path) in fetch_prov:
            doc["provenance"]["retrieval"] = fetch_prov[str(path)]
        doc["provenance"]["input_detected_as"] = input_summary["detected_as"]
        base = out_dir / f"{path.stem}_abcd"
        paths = abcd_export.write_all(doc, base, kind="paper", formats=formats)
        v = doc["verification"]
        print(f"{path.name}: {v['variables_dictionary_verified']} verified vars, "
              f"{len(doc['findings'])} findings, {v['rejected_total']} rejected "
              f"-> {', '.join(str(p.name) for p in paths.values())}")
        written_docs.append(doc)

    if do_synth and written_docs:
        from scripts.abcd_synthesize import synthesize

        syn = synthesize(written_docs)
        paths = abcd_export.write_all(syn, out_dir / "abcd_synthesis",
                                      kind="synthesis", formats=formats)
        t = syn["totals"]
        print(f"synthesis: {t['papers']} papers, {t['consensus_constructs']} consensus, "
              f"{t['divergent_constructs']} divergent -> "
              f"{', '.join(str(p.name) for p in paths.values())}")

    if failures:
        print(f"\n{len(failures)} of {len(inputs)} inputs failed:", file=sys.stderr)
        for p, why in failures:
            print(f"  {p}: {why}", file=sys.stderr)
        return 2 if written_docs else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
