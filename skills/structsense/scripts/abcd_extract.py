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

ONE ARGUMENT, no mode flags. The input is auto-detected (see `abcd_inputs`): a PDF,
a directory, a CSV/TSV/XLSX of DOIs, a DOI list, or a bare DOI. More than one paper
implies a cross-paper synthesis (--no-synthesize to skip).

WHO RUNS THE MODEL — two paths, pick by where you are:

  A. **You are the model** (Claude Code, Codex, Claude Desktop, any agent reading
     this skill). Do NOT pass --llm-model; there is no API to call. Two steps:

         python -m scripts.abcd_extract ./papers --prepare
         # -> extracts text to <stem>.txt per paper and prints the plan.
         # Read each text, follow prompts/extractor-abcd.md yourself, write the
         # payload JSON next to it as <stem>.payload.json, then:
         python -m scripts.abcd_extract ./papers --payload ./papers

     Verification, dictionary gating, construct mapping, synthesis and all three
     output formats are identical on this path — they are scripts, not prompts.
     Being the model does not exempt you from the quote rule; the verifier will
     delete anything you cannot support.

  B. **A framework calls an API for you** (Pi, a cron job, a batch runner). Pass
     --llm-model and it does the extraction itself:

         python -m scripts.abcd_extract ./papers --llm-model openai/gpt-4o-mini

Passing neither is an error, because guessing would either burn API credits you did
not ask for or silently produce nothing.
"""
from __future__ import annotations

# Run either way: `python -m scripts.<mod>` from the skill directory, or
# `python /abs/path/to/scripts/<mod>.py` from anywhere. Without this, running the
# file directly fails with ModuleNotFoundError: scripts — which forces callers to
# cd into the skill first, for no reason.
if __package__ in (None, ""):  # executed as a file, not as part of the package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scripts import abcd_context, abcd_export, abcd_inputs
from scripts.abcd_dictionary import Dictionary, DictionaryError
from scripts.abcd_verify import verify_payload
from scripts.cognitive_atlas import CognitiveAtlas, CognitiveAtlasError

SKILL_VERSION = "0.6.1"
# Results live here, beside the input rather than inside it. Overridable with
# --out-dir; excluded from input scanning so a rerun does not read its own output.
DEFAULT_OUT_DIRNAME = "abcd_results"
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


DOC_LEVEL_FIELDS = ("study", "data_release", "paper_title", "doi", "sample_size",
                    "design", "timepoints", "cohort", "site_count", "analytic_sample",
                    "preregistered", "data_source")

# Live NDA full-text search is one request per unplaceable mention. Fine for a few
# papers, thousands of requests for a corpus — so `--nda-api auto` only searches
# below this paper count. Printed-name confirmation is cheap and always allowed.
NDA_SEARCH_PAPER_BUDGET = 25


def _payload_meta(payload: dict) -> Dict[str, Any]:
    """Document-level fields from an agent-supplied payload.

    Accepts them at the top level (where `prompts/extractor-abcd.md` puts them) or
    nested under `source_metadata` (where a previous run's output has them), so
    both a fresh payload and a re-verified result behave the same.
    """
    meta: Dict[str, Any] = {}
    for key in DOC_LEVEL_FIELDS:
        if payload.get(key) not in (None, ""):
            meta[key] = payload[key]
    nested = payload.get("source_metadata")
    if isinstance(nested, dict):
        for key, val in nested.items():
            if val not in (None, ""):
                meta.setdefault(key, val)
    return meta


# --------------------------------------------------------------------------- #
# one paper
# --------------------------------------------------------------------------- #

def extract_paper(path: Path, *, llm_model: str, dictionary: Optional[Dictionary],
                  atlas: Optional[CognitiveAtlas], grobid_url: Optional[str] = None,
                  payload_override: Optional[dict] = None,
                  context_index: Optional[Any] = None,
                  nda: Optional[Any] = None,
                  study: Optional[str] = None) -> dict:
    """Extract + verify one paper. `payload_override` skips the LLM (re-verify)."""
    started = time.time()
    text, extractor = load_text(path, grobid_url=grobid_url)
    if not text.strip():
        raise RuntimeError(f"{path} produced no text — is it a scanned PDF? "
                           "Run OCR first, or pass a .txt sidecar.")

    if payload_override is not None:
        merged = {k: payload_override.get(k) or [] for k in SECTIONS}
        # Read document-level fields the same way as the API path. The extractor
        # prompt puts study / data_release / paper_title / doi at the TOP level, so
        # looking only in source_metadata silently dropped them — a paper stating
        # "ABCD 4.0" then verified with no release recorded, which is exactly the
        # provenance a cross-era comparison depends on.
        merged["_meta"] = _payload_meta(payload_override)
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
    # Which dictionary release this paper should be matched against. A 5.0 paper's
    # `nihtbx_cryst_fc` was renamed wholesale in 6.0, so matching it against every
    # loaded release turns one clear measure into rival candidates in two tables.
    releases = None
    if dictionary is not None:
        releases = abcd_context.releases_for_paper(
            meta.get("data_release"),
            sorted({s["release"] for s in dictionary.snapshots})) or None
    # NDA labels its structures by release only through 4.0, and the `nda-legacy`
    # snapshot is the union of 2.0/3.0/4.0 (116,353 variables, of which 87,682
    # existed in 3.0). When a paper names one of those releases, each row's
    # `nda_releases` makes the check exact instead of "somewhere in the NDA era".
    nda_release = abcd_context.nda_release_for_paper(meta.get("data_release"))
    verified = verify_payload(merged, text, dictionary=dictionary, atlas=atlas,
                              context_index=context_index, nda=nda,
                              releases=releases, nda_release=nda_release,
                              study=study)

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
            # Who actually did the extraction. "agent" means the calling agent was
            # the model (Claude Code / Codex path) — no API was involved.
            "llm_model": llm_model if payload_override is None else "agent (no API call)",
            "extraction_path": "api" if payload_override is None else "agent_supplied_payload",
            "text_extractor": extractor,
            "text_chars": len(text),
            "chunks": chunks_used,
            "elapsed_sec": round(time.time() - started, 2),
            "dictionaries": dictionary.provenance if dictionary else [],
            "dd_releases_matched_against": releases or "all loaded",
            "nda_release_required": nda_release,
            "construct_vocabularies": atlas.provenance if atlas else [],
            "nda_api": ("consulted" if nda is not None else "not used"),
            "verification_policy": {
                "quote_must_appear_in_paper": True,
                "variable_name_must_appear_in_quote": True,
                "claim_must_be_this_study_not_cited_work": True,
                "findings_must_come_from_this_papers_results": True,
                "construct_id_tool_only": True,
                "variable_must_resolve_in_dictionary_to_be_canonical": True,
                "context_mapping_thresholds": abcd_context.decision_rule(),
            },
        },
    }
    return doc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _resolve_inputs(target: str, *, download_dir: Optional[Path],
                    email: Optional[str], limit: Optional[int],
                    ignore_dirs: Sequence[Path] = ()):
    """Auto-detect the input and return (papers, summary). No --bulk needed."""
    papers, summary = abcd_inputs.resolve(target, download_dir=download_dir,
                                          email=email, limit=limit,
                                          ignore_dirs=ignore_dirs)
    print(f"input {summary['input']!r} detected as {summary['detected_as']}: "
          f"{summary['papers_resolved']}/{summary['papers_total']} papers available",
          file=sys.stderr)
    if summary.get("duplicates_dropped"):
        print(f"  {summary['duplicates_dropped']} duplicate file(s) skipped "
              f"(identical content) — {summary['files_seen']} files, "
              f"{summary['papers_total']} unique papers", file=sys.stderr)
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
                    help="path B only: have the script call an API "
                         "(openai/gpt-4o-mini, anthropic/claude-sonnet-5, ollama/llama3). "
                         "Omit inside Claude Code / Codex — you are the model there.")
    ap.add_argument("--prepare", action="store_true",
                    help="path A step 1: extract text per paper and print the plan, "
                         "so the agent can do the extraction itself")
    ap.add_argument("--payload", type=Path,
                    help="path A step 2: agent-produced payload — a .json (single "
                         "paper), a directory of <stem>.payload.json, or a .jsonl "
                         "keyed by source_path. No LLM is called.")
    ap.add_argument("--study", default="abcd", choices=["abcd", "hbcd"])
    ap.add_argument("--dd-release", action="append", default=None,
                    help="restrict dictionary snapshots to these releases (repeatable)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help=f"where results go (default: <input>/{DEFAULT_OUT_DIRNAME})")
    ap.add_argument("--formats", default="json,md,ttl")
    ap.add_argument("--grobid-url", default=os.getenv("GROBID_URL"))
    ap.add_argument("--reverify", type=Path, default=None,
                    help="re-verify an existing *_abcd.json against its paper "
                         "(alias of --payload; no LLM is called)")
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
    ap.add_argument("--no-context-mapping", action="store_true",
                    help="skip label/context mapping — only literal variable names "
                         "count (faster, but most papers name no variable literally)")
    ap.add_argument("--nda-api", choices=["auto", "on", "off"], default="auto",
                    help="consult the NDA data-element API for mentions the offline "
                         "dictionary cannot place. auto (default): confirm printed "
                         "names always, full-text search only for runs of at most "
                         f"{NDA_SEARCH_PAPER_BUDGET} papers, since a large corpus "
                         "would mean thousands of requests. on: always. off: never.")
    a = ap.parse_args(argv)

    formats = [f.strip() for f in a.formats.split(",") if f.strip()]

    in_path = Path(a.input).expanduser()
    # Results go in their own directory, never mixed in with the PDFs. A corpus
    # directory otherwise ends up with four files per paper interleaved with the
    # papers themselves, and the next run has to tell them apart.
    base = (in_path if in_path.is_dir()
            else in_path.parent if in_path.exists() else Path.cwd())
    out_dir = a.out_dir or (base / DEFAULT_OUT_DIRNAME)
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    text_dir = out_dir / "text"
    payload_dir = out_dir / "payloads"


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

    context_index = None
    if dictionary is not None and not a.no_context_mapping:
        t0 = time.time()
        context_index = abcd_context.ContextIndex(dictionary)
        st = context_index.stats
        print(f"context index: {st['labels_indexed']:,} labels, "
              f"{st['instruments']:,} instruments "
              f"({time.time() - t0:.1f}s)", file=sys.stderr)

    atlas: Optional[CognitiveAtlas] = None
    try:
        atlas = CognitiveAtlas(offline=a.offline_atlas)
    except CognitiveAtlasError as exc:
        print(f"warning: {exc} — constructs will be reported unmapped", file=sys.stderr)

    payload_arg = a.payload or a.reverify
    if payload_arg is None and not a.prepare and not a.llm_model:
        # --prepare told the agent to write payloads into <out>/payloads, so look
        # there before complaining. Reading files that already exist costs nothing;
        # the refusal below exists to avoid spending API credits by accident, not to
        # make the caller retype a path this script chose.
        if any(payload_dir.glob("*.payload.json")):
            payload_arg = payload_dir
            print(f"using payloads from {payload_dir}", file=sys.stderr)
    if not a.prepare and payload_arg is None and not a.llm_model:
        print(
            "error: nothing to extract with. Either:\n"
            "  * you are the model (Claude Code / Codex): run --prepare, do the\n"
            "    extraction yourself against prompts/extractor-abcd.md, then re-run\n"
            "    with --payload <file|dir>; or\n"
            "  * a framework should call an API (Pi, batch): pass --llm-model.\n"
            "Refusing to guess: one choice spends API credits, the other does not.",
            file=sys.stderr)
        return 1

    papers, input_summary = _resolve_inputs(
        a.input, download_dir=a.download_dir, email=a.email, limit=a.limit,
        ignore_dirs=[out_dir])
    nda = None
    if a.nda_api != "off" and dictionary is not None:
        if a.nda_api == "on" or len(papers) <= NDA_SEARCH_PAPER_BUDGET:
            from scripts import abcd_nda_api

            nda = abcd_nda_api
            print(f"nda api: enabled ({a.nda_api}) — cached under "
                  f"{abcd_nda_api.CACHE_DIR}", file=sys.stderr)
        else:
            print(f"nda api: skipped — {len(papers)} papers exceeds the "
                  f"{NDA_SEARCH_PAPER_BUDGET}-paper budget for live lookups; "
                  "pass --nda-api on to force it", file=sys.stderr)
    inputs = [p.path for p in papers if p.path]
    fetch_prov = {str(p.path): p.provenance for p in papers if p.origin != "local"}
    if a.prepare:
        plan = []
        for path in inputs:
            try:
                text, extractor = load_text(path, grobid_url=a.grobid_url)
            except Exception as exc:
                plan.append({"paper": str(path), "error": str(exc)})
                continue
            sidecar = text_dir / f"{path.stem}.txt"
            if path.suffix.lower() != ".txt":
                try:
                    text_dir.mkdir(parents=True, exist_ok=True)
                    sidecar.write_text(text)
                except Exception:
                    sidecar = path
            plan.append({
                "paper": str(path),
                "text": str(sidecar),
                "chars": len(text),
                "chunks_if_api_path": len(_chunks(text, a.llm_model or "unknown")),
                "write_payload_to": str(payload_dir / f"{path.stem}.payload.json"),
            })
        payload_dir.mkdir(parents=True, exist_ok=True)
        print(json.dumps({
            "prompt": str(PROMPT_PATH),
            "out_dir": str(out_dir),
            "schema": str(PROMPT_PATH.parent.parent / "schemas" / "abcd-paper.schema.json"),
            "papers": plan,
            "next": ("Read each `text`, follow `prompt`, write the JSON payload to "
                     "`write_payload_to`, then re-run with --payload "
                     f"{payload_dir}."),
        }, indent=1))
        return 0

    override = None
    payload_map: Dict[str, dict] = {}
    if payload_arg is not None:
        pth = Path(payload_arg)
        if pth.is_dir() and not any(pth.glob("*.payload.json")) \
                and (pth / "payloads").is_dir():
            pth = pth / "payloads"      # pointed at the results dir, not its payloads
        if pth.is_dir():
            for cand in sorted(pth.rglob("*.payload.json")):
                try:
                    payload_map[cand.name.replace(".payload.json", "")] = \
                        json.loads(cand.read_text())
                except Exception as exc:
                    print(f"warning: {cand}: {exc}", file=sys.stderr)
            if not payload_map:
                print(f"error: no *.payload.json under {pth}", file=sys.stderr)
                return 1
        elif pth.suffix == ".jsonl":
            for line in pth.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = Path(str(rec.get("source_path") or rec.get("paper") or "")).stem
                if key:
                    payload_map[key] = rec
        else:
            override = json.loads(pth.read_text())
    # More than one paper implies a synthesis unless told otherwise.
    do_synth = a.synthesize if a.synthesize is not None else len(inputs) > 1

    written_docs: List[dict] = []
    failures: List[Tuple[Path, str]] = []
    for path in inputs:
        try:
            this_payload = override
            if payload_map:
                this_payload = payload_map.get(path.stem)
                if this_payload is None:
                    failures.append((path, "no payload provided for this paper"))
                    print(f"SKIPPED {path.name}: no payload "
                          f"(expected {path.stem}.payload.json)", file=sys.stderr)
                    continue
            doc = extract_paper(path, llm_model=a.llm_model, dictionary=dictionary,
                                atlas=atlas, grobid_url=a.grobid_url,
                                payload_override=this_payload,
                                context_index=context_index, nda=nda,
                                study=a.study)
        except Exception as exc:            # one bad paper must not stop a corpus
            failures.append((path, str(exc)))
            print(f"FAILED {path.name}: {exc}", file=sys.stderr)
            if len(inputs) == 1:
                return 1
            continue
        if str(path) in fetch_prov:
            doc["provenance"]["retrieval"] = fetch_prov[str(path)]
        doc["provenance"]["input_detected_as"] = input_summary["detected_as"]
        doc["provenance"]["input_duplicates_dropped"] = input_summary.get(
            "duplicates_dropped")
        base = out_dir / f"{path.stem}_abcd"
        paths = abcd_export.write_all(doc, base, kind="paper", formats=formats)
        v = doc["verification"]
        cov = doc.get("coverage") or {}
        print(f"{path.name}: {v['variables_mapped_any_method']}/"
              f"{len(doc['variables'])} variables mapped "
              f"({v['variables_with_table']} with a table), "
              f"{len(doc['findings'])} findings, {v['rejected_total']} rejected"
              + (f", {v['rejected_as_cited_work']} as cited work"
                 if v.get("rejected_as_cited_work") else "")
              + (f", {len(cov.get('referenced_but_not_declared') or [])} referenced "
                 "but undeclared" if cov.get("referenced_but_not_declared") else "")
              + f" -> {', '.join(str(p.name) for p in paths.values())}")
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

    if written_docs:
        print(f"results in {out_dir}")
    if failures:
        print(f"\n{len(failures)} of {len(inputs)} inputs failed:", file=sys.stderr)
        for p, why in failures:
            print(f"  {p}: {why}", file=sys.stderr)
        return 2 if written_docs else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
