"""Progressive, resumable corpus runner: many papers x one or more NER variants.

    "extract neuroscience and cell NER from ~/papers, save to ~/out"

    python -m scripts.batch init --input ~/papers --variants neuroscience,cns-cells \\
        --out ~/out --model <your model id>
    python -m scripts.batch next   --manifest ~/out/.structsense/batch.json   # host mode: repeat
    python -m scripts.batch run    --manifest ~/out/.structsense/batch.json   # headless
    python -m scripts.batch status --manifest ~/out/.structsense/batch.json

One state machine, two ways to drive it:

  next   (host-model mode: YOU are the model). Runs every deterministic stage it
         can, then prints ONE small task as JSON — one chunk to extract, one masked
         chunk to re-read, the kg_plan, one judge packet, the combiner — with the file
         to read and the file to write. Do it, write the file, run `next` again.
         Context stays small: nothing asks you to hold a whole paper, and every
         result is on disk before the next task starts.
  run    (headless). The same tasks, fulfilled by scripts/llm_client.py; inside
         Claude Code the default model is `claude-code` (the `claude` CLI), never
         OpenRouter unless you pass one.

Per paper and variant the deliverable is <variant out>/<stem>.ttl, written and
gated (validate_ttl) as soon as that paper finishes — not at the end. Work state
lives in <variant out>/.structsense/<stem>/; a re-run resumes from it, a paper
whose TTL already passed the gate is skipped, `retry` resets one. Several agents
may run `next --paper <stem>` (or plain `next`) concurrently: a task is claimed
by a .claim file and not handed out twice while the claim is fresh.

Variants: general, neuroscience, cns-cells (aliases: neuro, cell, cells,
cell-ner, cns), and resource (aliases: resources, bkr) — research resources as a
resource KG in the BrainKB Resource Ontology (prompts/extractor-resource.md ->
scripts/resource_kg.py; no kg_plan or judge stages; the corpus roll-up is one
merged resource KG, corpus_resource_kg.ttl). With one variant the output is --out;
with several, each gets --out/<variant>_output unless --out-map says otherwise.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
SKILL = _SCRIPTS.parent

VARIANTS = {"general": "general", "generic": "general",
            "neuroscience": "neuroscience", "neuro": "neuroscience",
            "cns-cells": "cns-cells", "cns_cells": "cns-cells", "cns": "cns-cells", "cell": "cns-cells",
            "cells": "cns-cells", "cell-ner": "cns-cells", "cell_ner": "cns-cells",
            "resource": "resource", "resources": "resource", "bkr": "resource"}
TEXT_SUFFIXES = {".txt", ".md"}
INPUT_SUFFIXES = {".pdf", ".txt", ".md", ".xml", ".docx", ".pptx", ".html", ".htm", ".xlsx", ".csv"}
CLAIM_TTL = 3600
MANIFEST_VERSION = 1


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(p: Path) -> str:
    return str(p)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def locked(manifest: Path):
    lock = manifest.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path}: no manifest — run `python -m scripts.batch init ...` first")
    return json.loads(path.read_text())


def save_manifest(path: Path, m: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)  # atomic: a crash never leaves half a manifest


def update_job(path: Path, key: str, **fields) -> None:
    with locked(path):
        m = load_manifest(path)
        m["jobs"].setdefault(key, {}).update(fields)
        save_manifest(path, m)


def resolve_inputs(raw: list[str], glob: Optional[str]) -> list[Path]:
    out: list[Path] = []
    for r in raw:
        p = Path(r).expanduser()
        if p.is_dir():
            pats = [g.strip() for g in (glob or "").split(",") if g.strip()]
            found = sorted({f for g in pats for f in p.glob(g)} if pats else
                           {f for f in p.iterdir() if f.suffix.lower() in INPUT_SUFFIXES})
            out.extend(f for f in found if f.is_file() and not f.name.startswith("."))
        elif p.is_file():
            out.append(p)
        else:
            raise SystemExit(f"{p}: no such file or directory")
    # one paper per stem: prefer the richest source (pdf/xml over a derived .txt)
    by_stem: dict[str, Path] = {}
    rank = {".xml": 0, ".pdf": 1, ".docx": 2, ".html": 3, ".htm": 3, ".md": 4, ".txt": 5}
    for p in out:
        cur = by_stem.get(p.stem)
        if cur is None or rank.get(p.suffix.lower(), 9) < rank.get(cur.suffix.lower(), 9):
            by_stem[p.stem] = p
    return [by_stem[s] for s in sorted(by_stem)]


def cmd_init(args) -> int:
    variants = []
    for v in args.variants.split(","):
        v = v.strip().lower()
        if not v:
            continue
        if v not in VARIANTS:
            raise SystemExit(f"unknown variant {v!r}; one of {sorted(set(VARIANTS.values()))} "
                             f"(aliases {sorted(VARIANTS)})")
        if VARIANTS[v] not in variants:
            variants.append(VARIANTS[v])
    out = Path(args.out).expanduser().resolve()
    out_map: dict[str, Path] = {}
    for kv in (args.out_map or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out_map[VARIANTS.get(k.strip().lower(), k.strip())] = Path(v).expanduser().resolve()
    vspec = {}
    for v in variants:
        vout = out_map.get(v) or (out if len(variants) == 1 else out / f"{v.replace('-', '_')}_output")
        vspec[v] = {"domain": v, "out": str(vout)}
        vout.mkdir(parents=True, exist_ok=True)
    papers = resolve_inputs(args.input, args.input_glob)
    if not papers:
        raise SystemExit("no input documents found")
    manifest = Path(args.manifest).expanduser().resolve() if args.manifest else out / ".structsense" / "batch.json"
    with locked(manifest):
        m = json.loads(manifest.read_text()) if manifest.is_file() else {
            "version": MANIFEST_VERSION, "created_at": utc_now(), "papers": [], "jobs": {}, "variants": {}}
        m["variants"].update(vspec)
        m["model"] = args.model or m.get("model")
        m["settings"] = {**(m.get("settings") or {}),
                         "chunk_chars": args.chunk_chars, "recall": not args.no_recall,
                         "judges": not args.no_judges, "keep_json": args.keep_json,
                         "entity_views": args.entity_views,
                         "mapper_url": args.mapper_url,
                         "mapping_sources": [x.strip() for x in (args.mapping_sources or "").split(",") if x.strip()] or None,
                         "loader": {"docling": not args.no_docling,
                                                                   "grobid": not args.no_grobid},
                         "text_dir": str(manifest.parent / "text")}
        known = {p["stem"] for p in m["papers"]}
        for p in papers:
            if p.stem not in known:
                m["papers"].append({"stem": p.stem, "input": str(p)})
        for p in m["papers"]:
            for v in m["variants"]:
                m["jobs"].setdefault(f"{p['stem']}::{v}", {"status": "pending"})
        save_manifest(manifest, m)
    print(json.dumps({"manifest": str(manifest), "papers": len(m["papers"]), "variants": vspec,
                      "next": f"python -m scripts.batch next --manifest {manifest}"}, indent=1))
    return 0


# ---------------------------------------------------------------------------
# per-job state
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, m: dict, manifest: Path, paper: dict, variant: str):
        self.m, self.manifest, self.paper, self.variant = m, manifest, paper, variant
        self.stem, self.input = paper["stem"], Path(paper["input"])
        self.key = f"{self.stem}::{variant}"
        self.domain = m["variants"][variant]["domain"]
        self.out = Path(m["variants"][variant]["out"])
        self.W = self.out / ".structsense" / self.stem
        self.ttl = self.out / f"{self.stem}.ttl"
        self.settings = m.get("settings") or {}
        self.model = m.get("model") or "unknown"

    @property
    def state(self) -> dict:
        return self.m["jobs"].setdefault(self.key, {"status": "pending"})

    def set(self, **fields) -> None:
        self.state.update(fields)
        update_job(self.manifest, self.key, **fields)

    # files
    @property
    def text_path(self) -> Path:
        return Path(self.settings.get("text_dir") or self.manifest.parent / "text") / f"{self.stem}.txt"

    def f(self, *parts) -> Path:
        return self.W.joinpath(*parts)

    @property
    def final_json(self) -> Path:
        return self.f(f"{self.stem}_final.json")


def read_json(p: Path) -> Optional[Any]:
    if not p.is_file():
        return None
    from json_repair import parse_or_repair
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return parse_or_repair(p.read_text(errors="replace"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str) + "\n")
    tmp.replace(p)


def claimed(target: Path) -> bool:
    c = target.with_suffix(target.suffix + ".claim")
    return c.is_file() and time.time() - c.stat().st_mtime < CLAIM_TTL


def claim(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix(target.suffix + ".claim").write_text(utc_now())


def release(target: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        target.with_suffix(target.suffix + ".claim").unlink()


def accept(target: Path, check) -> Optional[Any]:
    """The agent's file, if it exists and passes `check`; a bad one is moved aside
    (target.bad) so the task is handed out again with the reason."""
    obj = read_json(target)
    if obj is None:
        if target.is_file():
            target.replace(target.with_suffix(target.suffix + ".bad"))
        return None
    why = check(obj)
    if why:
        target.with_suffix(target.suffix + ".bad").write_text(json.dumps({"why": why}) + "\n")
        target.unlink()
        return None
    release(target)
    return obj


def previous_error(target: Path) -> Optional[str]:
    bad = target.with_suffix(target.suffix + ".bad")
    if bad.is_file():
        try:
            return json.loads(bad.read_text()).get("why") or "previous file was not valid JSON"
        except json.JSONDecodeError:
            return "previous file was not valid JSON"
    return None


# ---------------------------------------------------------------------------
# deterministic stages
# ---------------------------------------------------------------------------

def ensure_text(job: Job) -> str:
    tp = job.text_path
    if tp.is_file() and tp.stat().st_size:
        return tp.read_text(encoding="utf-8")
    tp.parent.mkdir(parents=True, exist_ok=True)
    src = job.input
    if src.suffix.lower() in TEXT_SUFFIXES:
        text = src.read_text(encoding="utf-8", errors="replace")
    elif src.suffix.lower() == ".xml":
        from fetch_fulltext import jats_to_text
        text = jats_to_text(src.read_bytes()) or ""
    else:
        from input_loader import process_file
        loader = job.settings.get("loader") or {}
        text = process_file(src, prefer_grobid=loader.get("grobid", True), use_docling=loader.get("docling", True))
    if not text.strip():
        raise RuntimeError(f"{src}: no text could be extracted")
    tp.write_text(text, encoding="utf-8")
    write_json(tp.with_suffix(".ingest.json"), ingestion_record(src, tp, text, job.settings.get("loader") or {}))
    return text


def ingestion_record(src: Path, tp: Path, text: str, loader: dict) -> dict:
    """How the processed text came to be: provenance for the TTL (source document ->
    DocumentIngestionActivity -> extracted text)."""
    import hashlib
    import input_loader
    backend = {".txt": "plain", ".md": "plain", ".xml": "jats"}.get(src.suffix.lower()) or input_loader.LAST_BACKEND
    version = None
    pkg = {"pymupdf4llm": "pymupdf4llm", "pymupdf": "pymupdf", "pdfminer": "pdfminer.six", "docling": "docling"}.get(backend or "")
    if pkg:
        with contextlib.suppress(Exception):
            from importlib.metadata import version as _v
            version = f"{backend} {_v(pkg)}"
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    rec = {"backend": backend, "backend_version": version, "chars": len(text), "at": utc_now(),
           "source_sha256": sha(src.read_bytes()), "text_sha256": sha(tp.read_bytes()),
           "docling": loader.get("docling", True), "grobid": loader.get("grobid", True)}
    if src.suffix.lower() == ".pdf":
        from doc_metadata import from_pdf
        rec["page_count"] = from_pdf(src).get("page_count")
    return rec


def chunks_for(job: Job, text: str, size: Optional[int] = None) -> list[dict]:
    p = job.f("chunks.json")
    got = read_json(p)
    if isinstance(got, list) and got:
        return got
    from chunking import chunk_by_sentences
    size = int(size or job.settings.get("chunk_chars") or 12000)
    cs = chunk_by_sentences(text, max_chars=size, overlap_sentences=1)
    out = [{"i": i, "start": c["start"], "end": c["start"] + len(c["text"])} for i, c in enumerate(cs)]
    write_json(p, out)
    for c in out:
        write_text(job.f("chunks", f"chunk-{c['i']:03d}.txt"), text[c["start"]:c["end"]])
    return out


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def check_extraction(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return "expected a JSON object with an `entities` list"
    if not isinstance(obj.get("entities"), list):
        return "missing `entities` list (write {\"entities\": []} if the chunk has none)"
    bad = [e for e in obj["entities"] if not (isinstance(e, dict) and e.get("entity") and e.get("label"))]
    if bad:
        return f"{len(bad)} entities lack `entity` or `label`"
    return None


def check_resource_extraction(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict) or "extracted_resources" not in obj:
        return "expected {\"extracted_resources\": [ ... ]} (an empty list if the chunk names no resource)"
    er = obj["extracted_resources"]
    recs = [r for g in er.values() for r in (g or [])] if isinstance(er, dict) else er
    if not isinstance(recs, list):
        return "`extracted_resources` must be a list of records"
    bad = [r for r in recs if not (isinstance(r, dict) and r.get("name") and (r.get("extracted_type") or r.get("type")))]
    if bad:
        return f"{len(bad)} record(s) lack `name` or `extracted_type`"
    return None


def check_recall(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict) or not isinstance(obj.get("missed_entities", obj.get("entities")), list):
        return "expected {\"missed_entities\": [...]} (empty list if nothing was missed)"
    return None


def anchor(items: list[dict], text: str, chunk_start: int) -> list[dict]:
    """Chunk-local (or absolute) offsets -> document offsets, verified; an item whose
    offsets select nothing keeps no offsets (it still counts as a lexicon entry)."""
    out = []
    for it in items:
        it = dict(it)
        s, e, surf = it.get("start"), it.get("end"), it.get("entity") or it.get("term")
        if isinstance(s, int) and isinstance(e, int) and surf:
            if text[chunk_start + s:chunk_start + e] == surf:
                it["start"], it["end"] = chunk_start + s, chunk_start + e
            elif text[s:e] != surf:
                it.pop("start", None), it.pop("end", None)
        out.append(it)
    return out


def build_result(job: Job, text: str, chunks: list[dict]) -> dict:
    from expand_mentions import expand
    entries, kts, causal, meta = [], [], [], {}
    for c in chunks:
        for sub in ("extract", "recall"):
            obj = read_json(job.f(sub, f"part-{c['i']:03d}.json"))
            if not isinstance(obj, dict):
                continue
            ents = obj.get("entities") if sub == "extract" else obj.get("missed_entities", obj.get("entities"))
            if sub == "recall":  # offsets are into the MASKED chunk: keep surface + label only
                ents = [{k: v for k, v in e.items() if k not in ("start", "end", "sentence")}
                        for e in ents or [] if isinstance(e, dict) and not str(e.get("entity", "")).startswith("[E")]
            entries.extend(anchor([e for e in ents or [] if isinstance(e, dict)], text, c["start"]))
            kts.extend(anchor([k for k in (obj.get("missed_key_terms", obj.get("key_terms")) if sub == "recall" else obj.get("key_terms")) or [] if isinstance(k, dict)], text, c["start"]))
            causal.extend(cr for cr in obj.get("causal_relations") or [] if isinstance(cr, dict))
            for k, v in (obj.get("source_metadata") or {}).items():
                if v and not meta.get(k):
                    meta[k] = v
    lex = {"entries": entries, "items": [e for e in entries if isinstance(e.get("start"), int)]}
    items, rep = expand(lex, text, model=job.model, keep_nested=job.domain == "cns-cells")
    kts = [k for k in kts if isinstance(k.get("start"), int)]
    from doc_metadata import harvest, merge
    meta = merge(meta, harvest(job.input, text))
    meta["source_path"] = str(job.input)
    meta["text_path"] = str(job.text_path)
    ing = read_json(job.text_path.with_suffix(".ingest.json"))
    if isinstance(ing, dict):
        meta["ingestion"] = ing
    result = {"source_metadata": meta, "task_type": "ner", "ner_domain": job.domain,
              "entities": items, "key_terms": kts, "causal_relations": causal,
              "expansion": {k: (v[:50] if isinstance(v, list) else v) for k, v in rep.items()}}
    return result


def map_and_normalize(job: Job, result: dict, text: str, n_chunks: int) -> dict:
    from concept_mapping import ConceptMapper, map_result
    from normalize_result import normalize
    cm = ConceptMapper(sources=job.settings.get("mapping_sources") or None,
                       local_url=job.settings.get("mapper_url") or None, ask_user=None)
    if not cm.usable_sources():
        raise RuntimeError("concept mapping is mandatory and tool-only (rule 15) and no source in "
                           "concept_mapping.json is usable: run `python -m scripts.concept_mapping index` "
                           "(trusted ontologies, offline), start the local mapper, or set BIOPORTAL_API_KEY")
    map_result(result, cm, texts=[text])
    normalize(result, llm_model=job.model, input_path=str(job.input), input_text_chars=len(text),
              chunk_size_chars=int(job.settings.get("chunk_chars") or 12000), chunk_count=n_chunks)
    return result


def mask_chunk(text: str, chunk: dict, items: list[dict]) -> tuple[str, list[dict]]:
    a, b = chunk["start"], chunk["end"]
    spans = sorted({(it["start"], it["end"]) for it in items
                    if isinstance(it.get("start"), int) and a <= it["start"] and it["end"] <= b})
    out, pos, n, legend = [], a, 0, {}
    for s, e in spans:
        if s < pos:
            continue
        out.append(text[pos:s])
        out.append(f"[E{n}]")
        legend.setdefault(text[s:e], n)
        n += 1
        pos = e
    out.append(text[pos:b])
    return "".join(out), [{"placeholder": f"[E{i}]", "entity": s} for s, i in list(legend.items())[:300]]


def kg_plan_input(result: dict, max_items: Optional[int] = None) -> dict:
    from group_by_entity import mention_groups
    items = []
    groups = sorted(mention_groups(result), key=lambda g: -len(g["items"]))  # the most-used entities first
    for g in groups[:max_items]:
        it = g["items"][0]
        items.append({"id": g["id"], "entity": g["surface"], "label": g["label"], "mentions": len(g["items"]),
                      "ontology_id": it.get("ontology_id") if it.get("concept_mapping_provenance") == "tool" else None,
                      "ontology_label": it.get("ontology_label"),
                      "sentences": list(dict.fromkeys(i.get("sentence") for i in g["items"] if i.get("sentence")))[:3]})
    return {"items": items}


def check_plan(obj: Any) -> Optional[str]:
    return None if isinstance(obj, dict) else "kg_plan must be a JSON object ({} is valid)"


def check_review(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return "expected {\"items\": [{\"id\", \"verdict\": pass|flag|fail, ...}]}"
    return None


def dedupe_review(obj: dict) -> dict:
    """An id reviewed twice in one file (a model repeating itself) keeps its first
    verdict; judge_combine rejects duplicates, and one repeat must not fail a paper."""
    seen, items = set(), []
    for it in obj.get("items") or []:
        if isinstance(it, dict) and it.get("id") not in seen:
            seen.add(it.get("id"))
            items.append(it)
    obj["items"] = items
    return obj


def check_combiner(obj: Any) -> Optional[str]:
    return None if isinstance(obj, dict) else "expected the JSON object prompts/judge-combiner.md specifies"


def drop_generic_keys(job: Job, plan: Optional[dict]) -> Optional[dict]:
    """A kg_plan key on the generic_keys guardrail ('mouse', 'neuron') would fail the
    gate and lose the paper. Drop it (json_to_ttl then derives the key from the
    trusted ontology's preferred label or the algorithm) and record that it happened."""
    if not plan or not isinstance(plan.get("entities"), dict):
        return plan
    cfg = json.loads((SKILL / "default_ontology" / "ttl_config.json").read_text())
    generic = set(cfg.get("generic_keys") or [])
    dropped = []
    for gid, e in plan["entities"].items():
        if isinstance(e, dict) and e.get("normalized_key") in generic:
            dropped.append(f"{gid} ({e.pop('normalized_key')})")
    if dropped:
        job.set(generic_keys_dropped=dropped[:50])
        print(f"  {job.key}: dropped {len(dropped)} generic kg_plan key(s): {dropped[:5]}", file=sys.stderr)
    return plan


def finish(job: Job, result: dict, plan: Optional[dict]) -> dict:
    from json_to_ttl import result_to_ttl
    from validate_ttl import validate_file
    started = job.state.get("started_at") or utc_now()
    variant = "resource" if job.domain == "resource" else f"ner:{job.domain}"
    result["run_metadata"] = {"started_at": started, "ended_at": utc_now(), "extractor_model": job.model,
                              "judge_model": None if job.domain == "resource" or not job.settings.get("judges", True)
                              else job.model,
                              "mode": job.state.get("mode") or "host_sequential",
                              "variant": variant, **({} if job.domain == "resource" else {"ner_domain": job.domain})}
    write_json(job.final_json, result)
    ttl, conv = result_to_ttl(result, kg_plan=plan, source_path=job.text_path, variant=variant)
    job.ttl.write_text(ttl)
    gate = validate_file(job.ttl)
    if gate["ok"]:
        views = {}
        if job.settings.get("entity_views"):  # one TTL per paper by default
            from entity_view import write_entity_views
            views = write_entity_views(ttl, job.ttl)
        with contextlib.suppress(FileNotFoundError):
            job.ttl.with_suffix(".invalid.ttl").unlink()
        counts = conv["counts"]
        extra = ({"resources": counts.get("resources", 0), "records": counts.get("records", 0),
                  "source_silence_findings": gate.get("source_silence_findings", 0)}
                 if conv.get("kind") == "resource_kg" else
                 {"mentions": counts.get("mentions", 0), "entities": counts.get("entities", 0)})
        return {"status": "done", "ttl": str(job.ttl), **views, "triples": conv["triples"], **extra,
                "warnings": gate["warning_count"], "ended_at": utc_now()}
    bad = job.ttl.with_suffix(".invalid.ttl")
    job.ttl.replace(bad)
    top = {k: list(v)[:3] for k, v in list(gate["violations"].items())[:6]}
    return {"status": "invalid", "ttl": str(bad), "violations": top, "ended_at": utc_now()}


# ---------------------------------------------------------------------------
# the state machine: advance one job until it needs the model
# ---------------------------------------------------------------------------

def prompt_path(name: str) -> str:
    return str(SKILL / "prompts" / f"{name}.md")


def advance(job: Job) -> Optional[dict]:
    """Run deterministic stages; return the next task for the model, or None when the
    job is finished (done/invalid/failed) or everything left is claimed by others."""
    st = job.state
    if st.get("status") in ("done", "invalid", "failed"):
        return None
    if job.ttl.is_file() and st.get("status") != "running":
        from validate_ttl import validate_file
        if validate_file(job.ttl)["ok"]:  # resume: already delivered and valid
            job.set(status="done", ttl=str(job.ttl), note="existing TTL passed the gate; skipped")
            return None
    if not st.get("started_at"):
        job.set(started_at=utc_now(), status="running")
    text = ensure_text(job)
    if job.domain == "resource":
        return advance_resource(job, text)
    chunks = chunks_for(job, text)
    domain = job.domain
    extractor = prompt_path(f"extractor-ner-{domain}")

    # 1. extraction, one chunk at a time
    for c in chunks:
        target = job.f("extract", f"part-{c['i']:03d}.json")
        if accept(target, check_extraction) is not None:
            continue
        if claimed(target):
            continue
        claim(target)
        return {"task": "extract", "job": job.key, "paper": job.stem, "variant": domain,
                "prompt": extractor, "read": str(job.f("chunks", f"chunk-{c['i']:03d}.txt")),
                "chunk": f"{c['i'] + 1}/{len(chunks)}", "write": str(target),
                "retry_reason": previous_error(target),
                "instructions": (
                    "Follow the prompt's System block on this chunk. Write its JSON (entities; key_terms; "
                    "optional causal_relations only if requested) to `write`; offsets may be chunk-local. Emit each "
                    "occurrence with its own context, identity and evidence-bearing relations. Expansion can "
                    "recover plain repeated surfaces but cannot recover context-specific claims. "
                    "Chunk 1: also fill source_metadata (paper_title, doi, year, "
                    "journal, authors as [{name, orcid?}] in printed order) — only what the text states. "
                    "Skip references, acknowledgements, funding/grant numbers and author lists.")}

    # 2. expansion + (optional) mask-recall, again per chunk
    base = job.f("expanded.json")
    if not base.is_file():
        write_json(base, build_result(job, text, chunks))
    if job.settings.get("recall", True):
        items = (read_json(base) or {}).get("entities") or []
        for c in chunks:
            target = job.f("recall", f"part-{c['i']:03d}.json")
            if accept(target, check_recall) is not None:
                continue
            if claimed(target):
                continue
            masked, legend = mask_chunk(text, c, items)
            mp = job.f("recall", f"masked-{c['i']:03d}.txt")
            write_text(mp, masked)
            write_json(job.f("recall", f"legend-{c['i']:03d}.json"), legend)
            claim(target)
            return {"task": "recall", "job": job.key, "paper": job.stem, "variant": domain,
                    "prompt": prompt_path("mask-recall-pass"), "label_set_from": extractor,
                    "read": str(mp), "legend": str(job.f("recall", f"legend-{c['i']:03d}.json")),
                    "chunk": f"{c['i'] + 1}/{len(chunks)}", "write": str(target),
                    "retry_reason": previous_error(target),
                    "instructions": ("Mask-recall pass: [En] tokens are already extracted. Write "
                                     "{\"missed_entities\": [{entity, label}]} for what pass 1 missed, "
                                     "using the label set of `label_set_from`; one item per distinct "
                                     "surface is enough. Empty list if nothing was missed.")}

    # 3. merge, map, normalize (deterministic)
    mapped = job.f("mapped.json")
    if not mapped.is_file():
        result = build_result(job, text, chunks)
        write_json(mapped, map_and_normalize(job, result, text, len(chunks)))
    result = read_json(mapped)

    # 4. kg_plan (default step)
    plan_p = job.f("kg_plan.json")
    plan = accept(plan_p, check_plan)
    if plan is None:
        if claimed(plan_p):
            return None
        write_json(job.f("kg_plan_input.json"), kg_plan_input(result))
        claim(plan_p)
        return {"task": "kg_plan", "job": job.key, "paper": job.stem, "variant": domain,
                "prompt": prompt_path("kg-plan"), "read": str(job.f("kg_plan_input.json")),
                "source_text": str(job.text_path), "write": str(plan_p), "retry_reason": previous_error(plan_p),
                "instructions": ("Write kg_plan.json per the prompt, keyed by the item ids in `read`. Keys: "
                                 "references/key-normalization.md — specific referents, never a generic key "
                                 "and never a paper/DOI prefix to dodge the guardrail (leave a generic "
                                 "mention's key out instead). normalized_label is a short NAME; explanations go "
                                 "in `note`. Resolve entity identity first. Include only source-stated relations with verbatim evidence; "
                                 "do not add causal chains unless requested. "
                                 "{} is valid.")}
    from judge_ensemble import sanitize_kg_plan
    plan = sanitize_kg_plan(plan)

    # 5. judges, one packet at a time
    if job.settings.get("judges", True):
        jdir = job.f("judge")
        cfg = json.loads((SKILL / "judges_config.json").read_text())
        if not jdir.joinpath("packets").is_dir():
            from judge_prepare import prepare
            prepare(result, text, jdir, kg_plan=plan, cfg=cfg)
        for pk in sorted(jdir.glob("packets/*/part-*.json")):
            packet = json.loads(pk.read_text())
            target = jdir / packet["review_file"]
            if accept(target, check_review) is not None:
                continue
            if claimed(target):
                continue
            claim(target)
            j = packet["judge"]
            return {"task": "judge", "judge": j, "job": job.key, "paper": job.stem, "variant": domain,
                    "prompt": str(SKILL / cfg["judges"][j]["prompt"]), "read": str(pk),
                    "source_text": str(job.text_path) if j in ("grounding", "claims") else None,
                    "write": str(target), "retry_reason": previous_error(target),
                    "instructions": (f"You are ONLY the {j} judge: review this packet by the prompt and write "
                                     "{\"items\": [{\"id\", \"verdict\", \"confidence\", \"rationale\", "
                                     "\"suggestion\"?}]} for every item id. The packet's sentences usually "
                                     "settle it; open `source_text` only when they do not.")}
        from judge_combine import apply_combiner, combine, load_reviews
        reviews_paths = sorted((jdir / "reviews").glob("*.json"))
        for rp in reviews_paths:  # provenance: who judged, how
            r = dedupe_review(read_json(rp) or {})
            if r.get("judge") != "grounding_script":
                r.setdefault("judge", rp.stem.rsplit("-", 1)[0])
                r.setdefault("model", f"llm:{job.model}")
                r.setdefault("mode", job.state.get("mode") or "host_sequential")
                write_json(rp, r)
        judged, plan2, report = combine(result, load_reviews(reviews_paths), cfg, plan)
        if report.get("needs_review"):
            target = jdir / "combiner.json"
            dec = accept(target, check_combiner)
            if dec is None:
                if claimed(target):
                    return None
                write_json(jdir / "needs_review.json", {"needs_review": report["needs_review"]})
                claim(target)
                return {"task": "combiner", "job": job.key, "paper": job.stem, "variant": domain,
                        "prompt": prompt_path("judge-combiner"), "read": str(jdir / "needs_review.json"),
                        "write": str(target), "retry_reason": previous_error(target),
                        "instructions": "Choose among the judges' suggestions only; never invent a fix."}
            judged, plan2, log = apply_combiner(judged, dec, plan2, combiner_model=job.model)
        result, plan = judged, plan2

    # 6. Turtle + gate: this paper is delivered now, not at the end of the batch
    plan = drop_generic_keys(job, plan)
    # the plan AFTER judging (relabels moved its entries, kg-keys fixes applied):
    # what a later `retry --from-stage ttl` must render from, not the pre-judge draft
    write_json(job.f("kg_plan.final.json"), plan or {})
    res = finish(job, result, plan)
    job.set(**res)
    if res["status"] == "done" and not job.settings.get("keep_json"):
        for sub in ("chunks", "extract", "recall", "judge"):
            shutil.rmtree(job.f(sub), ignore_errors=True)
        for name in ("expanded.json", "mapped.json", "kg_plan_input.json"):
            with contextlib.suppress(FileNotFoundError):
                job.f(name).unlink()
    return None


def advance_resource(job: Job, text: str) -> Optional[dict]:
    """Resource variant: whole-document extraction (resource_kg_config.json
    extraction_chunk_chars), then deterministic merge -> grounding -> tool concept
    mapping (resource_kg.prepare) -> resource KG + gate. No kg_plan, no judges."""
    from resource_kg import load_config, prepare
    rcfg = load_config()
    chunks = chunks_for(job, text, size=int(rcfg.get("extraction_chunk_chars", 60000)))
    for c in chunks:
        target = job.f("extract", f"part-{c['i']:03d}.json")
        if accept(target, check_resource_extraction) is not None:
            continue
        if claimed(target):
            continue
        claim(target)
        return {"task": "extract", "job": job.key, "paper": job.stem, "variant": "resource",
                "prompt": prompt_path("extractor-resource"), "read": str(job.f("chunks", f"chunk-{c['i']:03d}.txt")),
                "chunk": f"{c['i'] + 1}/{len(chunks)}", "write": str(target),
                "retry_reason": previous_error(target),
                "instructions": (
                    "Follow the prompt's System block on this text. Write {\"extracted_resources\": [records]} to "
                    "`write` (schemas/bkr-resource-extraction.schema.json). Deep records for what the document "
                    "describes, short catalogue records with an observed scope for what it uses, `mentions` for "
                    "the rest. Every identifier, version, URL and licence must be written in the text; every "
                    "quote verbatim; no offsets; concept labels only, no IRIs; list not_found_fields. Skip "
                    "references, acknowledgements and funding. Chunk 1: also fill source_metadata (paper_title, "
                    "doi, year, journal) — only what the text states.")}
    records, meta = [], {}
    for c in chunks:
        obj = read_json(job.f("extract", f"part-{c['i']:03d}.json"))
        if not isinstance(obj, dict):
            continue
        er = obj.get("extracted_resources") or []
        records.extend(r for g in (er.values() if isinstance(er, dict) else [er]) for r in (g or []) if isinstance(r, dict))
        for k, v in (obj.get("source_metadata") or {}).items():
            if v and not meta.get(k):
                meta[k] = v
    from doc_metadata import harvest, merge
    meta = merge(meta, harvest(job.input, text))
    meta.update({"source_path": str(job.input), "text_path": str(job.text_path)})
    ing = read_json(job.text_path.with_suffix(".ingest.json"))
    if isinstance(ing, dict):
        meta["ingestion"] = ing
    result = {"source_metadata": meta, "task_type": "resource", "extracted_resources": records}
    from concept_mapping import ConceptMapper
    cm = ConceptMapper(sources=job.settings.get("mapping_sources") or None,
                       local_url=job.settings.get("mapper_url") or None, ask_user=None)
    if not cm.usable_sources():
        raise RuntimeError("concept mapping is mandatory and tool-only (rule 15) and no source in "
                           "concept_mapping.json is usable: run `python -m scripts.concept_mapping index` "
                           "or set BIOPORTAL_API_KEY")
    prepare(result, text, mapper=cm, cfg=rcfg)
    res = finish(job, result, None)
    job.set(**res)
    if res["status"] == "done" and not job.settings.get("keep_json"):
        for sub in ("chunks", "extract"):
            shutil.rmtree(job.f(sub), ignore_errors=True)
    return None


def jobs_in_order(m: dict, manifest: Path, paper: Optional[str]) -> list[Job]:
    out = []
    for p in m["papers"]:  # paper-major: every variant of a paper finishes before the next paper
        if paper and p["stem"] != paper:
            continue
        for v in m["variants"]:
            out.append(Job(m, manifest, p, v))
    return out


def next_task(manifest: Path, paper: Optional[str] = None, mode: str = "host_sequential") -> dict:
    m = load_manifest(manifest)
    for job in jobs_in_order(m, manifest, paper):
        if job.state.get("status") in ("done", "invalid", "failed"):
            continue
        if job.state.get("mode") != mode:
            job.set(mode=mode)
        try:
            task = advance(job)
        except Exception as exc:  # one bad paper never stops the batch
            job.set(status="failed", error=f"{type(exc).__name__}: {exc}", ended_at=utc_now())
            print(f"FAILED {job.key}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if task:
            task = {k: v for k, v in task.items() if v is not None}
            task["then"] = f"python -m scripts.batch next --manifest {manifest}" + (f" --paper {paper}" if paper else "")
            return task
        st = load_manifest(manifest)["jobs"].get(job.key, {})
        if st.get("status") in ("done", "invalid"):
            print(f"{st['status'].upper()} {job.key}: {st.get('ttl')}", file=sys.stderr)
    m = load_manifest(manifest)
    open_jobs = [k for k, j in m["jobs"].items() if j.get("status") not in ("done", "invalid", "failed")
                 and (not paper or k.startswith(paper + "::"))]
    if open_jobs:
        return {"task": "wait", "why": "remaining tasks are claimed by other workers", "open": open_jobs[:10],
                "then": f"python -m scripts.batch next --manifest {manifest}"}
    if not paper:
        rollup(m)
    return {"task": "done", "summary": summary(load_manifest(manifest))}


def rollup(m: dict) -> None:
    """Corpus view per variant (rule 9b), from the per-paper final JSON."""
    from merge_corpus import build_corpus, render_markdown
    for v, spec in m["variants"].items():
        out = Path(spec["out"])
        if v == "resource":
            rollup_resources(out)
            continue
        finals = sorted(out.glob(".structsense/*/*_final.json"))
        if len(finals) < 2:
            continue
        corpus = build_corpus(finals, include_mentions=False, with_index=True)
        (out / "corpus_synthesis.json").write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
        (out / "corpus_synthesis.md").write_text(render_markdown(corpus, top_n=50) + "\n")


def rollup_resources(out: Path) -> None:
    """Corpus resource KG: the union of the per-paper resource KGs (resources are one
    node across papers already), with mention stubs resolved across papers — a tool
    one paper only names joins the record another paper wrote. Gated like a paper."""
    import rdflib
    import bkr_stubs
    from resource_kg import print_validation, validate_file as validate_resource
    ttls = sorted(p for p in out.glob("*.ttl") if not p.name.endswith((".invalid.ttl", ".entities.ttl"))
                  and p.name != "corpus_resource_kg.ttl")
    if len(ttls) < 2:
        return
    g = rdflib.Graph()
    for p in ttls:
        g.parse(p, format="turtle")
    merged, _, kept = bkr_stubs.resolve(g)
    dest = out / "corpus_resource_kg.ttl"
    g.serialize(destination=str(dest), format="turtle")
    rep = validate_resource(dest)
    print(f"corpus resource KG: {dest} — {len(ttls)} papers, {len(g)} triples, {merged} cross-paper stub(s) "
          f"merged, {len(kept)} kept", file=sys.stderr)
    print_validation(rep)


def summary(m: dict) -> dict:
    by = {}
    for k, j in m["jobs"].items():
        by.setdefault(j.get("status", "pending"), []).append(k)
    return {"counts": {k: len(v) for k, v in by.items()},
            "invalid": {k: m["jobs"][k].get("violations") for k in by.get("invalid", [])},
            "failed": {k: m["jobs"][k].get("error") for k in by.get("failed", [])}}


# ---------------------------------------------------------------------------
# headless: fulfil the same tasks with an LLM
# ---------------------------------------------------------------------------

def fulfil(task: dict, model: str) -> None:
    from judge_ensemble import system_prompt
    from json_repair import parse_or_repair
    from llm_client import call
    t = task["task"]
    read = Path(task["read"]).read_text(encoding="utf-8")
    if t == "recall":
        label_block = system_prompt(str(Path(task["label_set_from"]).relative_to(SKILL)))
        system = system_prompt("prompts/mask-recall-pass.md").replace("{label_taxonomy_block}", label_block)
        user = f"MASKED INPUT TEXT:\n<<<\n{read}\n>>>\n\nWHAT THE PLACEHOLDERS REPLACED:\n" \
               f"{Path(task['legend']).read_text()}"
    else:
        system = system_prompt(str(Path(task["prompt"]).relative_to(SKILL)))
        if t == "extract":
            user = f"INPUT TEXT:\n<<<\n{read}\n>>>\n\nNOTE: {task['instructions']}"
        elif t == "judge":
            user = f"PACKET:\n{read}"
            if task.get("source_text"):
                user += f"\n\nSOURCE TEXT:\n{Path(task['source_text']).read_text(encoding='utf-8')}"
        else:
            user = read
    raw = call(model=model, system=system, user=user, json_mode=True, temperature=0, max_tokens=16000)
    obj = parse_or_repair(raw)
    if obj is None:
        obj = {"error": "model output was not JSON"}
    if t == "judge" and isinstance(obj, dict):
        obj.update({"judge": task["judge"], "model": f"llm:{model}", "mode": "parallel"})
    write_json(Path(task["write"]), obj)


def cmd_run(args) -> int:
    from llm_client import default_model
    manifest = Path(args.manifest).expanduser().resolve()
    model = args.model or default_model() or load_manifest(manifest).get("model")
    if not model or model == "unknown":
        raise SystemExit("no model: pass --model (inside Claude Code the default is `claude-code`), "
                         "or drive the batch yourself with `next` (host-model mode)")
    with locked(manifest):
        m = load_manifest(manifest)
        m["model"] = model
        save_manifest(manifest, m)
    while True:
        task = next_task(manifest, args.paper, mode="parallel")
        if task["task"] == "done":
            print(json.dumps(task, indent=1))
            return 0 if not task["summary"]["counts"].get("failed") else 2
        if task["task"] == "wait":
            time.sleep(30)
            continue
        print(f"[{task['job']}] {task['task']} {task.get('chunk') or task.get('judge') or ''}", file=sys.stderr)
        try:
            fulfil(task, model)
        except Exception as exc:
            write_json(Path(task["write"]), {"error": f"{type(exc).__name__}: {exc}"})
            print(f"  model call failed: {exc}", file=sys.stderr)


def cmd_next(args) -> int:
    task = next_task(Path(args.manifest).expanduser().resolve(), args.paper)
    print(json.dumps(task, indent=1, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    m = load_manifest(Path(args.manifest).expanduser().resolve())
    rows = []
    for k, j in m["jobs"].items():
        rows.append(f"{j.get('status', 'pending'):8} {k[:90]:90} {j.get('triples') or ''} {j.get('error') or ''}")
    print("\n".join(sorted(rows)))
    print(json.dumps(summary(m)["counts"]))
    return 0


def rerender(job: Job) -> dict:
    """Regenerate a finished paper's TTL from its kept final JSON + kg_plan, with the
    document metadata re-read (doc_metadata) — no model call, no re-extraction."""
    result = read_json(job.final_json)
    if not isinstance(result, dict):
        raise SystemExit(f"{job.key}: no {job.final_json.name} to re-render from; use --from-stage extract")
    from doc_metadata import harvest, merge
    text = job.text_path.read_text(encoding="utf-8") if job.text_path.is_file() else ""
    meta = merge(result.get("source_metadata"), harvest(job.input, text))
    meta.update({"source_path": str(job.input), "text_path": str(job.text_path)})
    ing = read_json(job.text_path.with_suffix(".ingest.json"))
    if not isinstance(ing, dict) and job.text_path.is_file():
        ing = ingestion_record(job.input, job.text_path, text, job.settings.get("loader") or {})
        ing["backend"] = ing.get("backend") or "unknown (text extracted before ingestion was recorded)"
        write_json(job.text_path.with_suffix(".ingest.json"), ing)
    if isinstance(ing, dict):
        meta["ingestion"] = ing
    result["source_metadata"] = meta
    from judge_ensemble import sanitize_kg_plan
    final = job.f("kg_plan.final.json")
    plan = sanitize_kg_plan(read_json(final if final.is_file() else job.f("kg_plan.json")) or {})
    return finish(job, result, drop_generic_keys(job, plan))


def cmd_retry(args) -> int:
    manifest = Path(args.manifest).expanduser().resolve()
    m = load_manifest(manifest)
    keys = [k for k in m["jobs"] if k.split("::")[0] == args.stem and (not args.variant or
            k.split("::")[1] == VARIANTS.get(args.variant, args.variant))]
    if not keys:
        raise SystemExit(f"no job for {args.stem}")
    for k in keys:
        stem, v = k.split("::")
        job = Job(m, manifest, next(p for p in m["papers"] if p["stem"] == stem), v)
        if args.from_stage in ("all", "extract"):
            shutil.rmtree(job.W, ignore_errors=True)
        elif args.from_stage == "map":
            # re-expand + re-map + re-judge from the kept extraction; kg_plan.json stays
            if not job.f("extract").is_dir():
                raise SystemExit(f"{k}: extraction parts were cleaned up (run without --keep-json); "
                                 f"use --from-stage extract")
            shutil.rmtree(job.f("judge"), ignore_errors=True)
            for n in ("mapped.json", "kg_plan.final.json", "kg_plan_input.json", f"{stem}_final.json"):
                with contextlib.suppress(FileNotFoundError):
                    job.f(n).unlink()
        elif args.from_stage == "judge":
            shutil.rmtree(job.f("judge"), ignore_errors=True)
        elif args.from_stage == "kg_plan":
            shutil.rmtree(job.f("judge"), ignore_errors=True)
            for n in ("kg_plan.json",):
                with contextlib.suppress(FileNotFoundError):
                    job.f(n).unlink()
        if args.from_stage == "ttl" and job.final_json.is_file():
            res = rerender(job)
            update_job(manifest, k, **res)
            print(f"{res['status'].upper()} {k}: {res.get('ttl')}")
            continue
        for p in (job.ttl, job.ttl.with_suffix(".invalid.ttl")):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        update_job(manifest, k, status="pending", error=None, violations=None, started_at=None)
    print(f"{len(keys)} job(s) handled from stage {args.from_stage}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init", help="create/extend a batch")
    a.add_argument("--input", action="append", required=True, help="file or directory (repeatable)")
    a.add_argument("--input-glob", default=None, help="for directories, e.g. '*.pdf,*.xml' (default: all documents)")
    a.add_argument("--variants", default="neuroscience", help="comma list: general, neuroscience, cns-cells")
    a.add_argument("--out", required=True)
    a.add_argument("--out-map", default=None, help="per-variant output dirs, e.g. neuroscience=/o/ner,cns-cells=/o/cell")
    a.add_argument("--manifest", default=None, help="default: <out>/.structsense/batch.json")
    a.add_argument("--model", default=None, help="model id for provenance (host mode: your own id)")
    a.add_argument("--chunk-chars", type=int, default=12000)
    a.add_argument("--no-recall", action="store_true", help="skip the mask-recall pass")
    a.add_argument("--no-judges", action="store_true", help="skip the judge ensemble (not recommended)")
    a.add_argument("--keep-json", action="store_true", help="keep chunk/judge work files after a paper is done")
    a.add_argument("--entity-views", action="store_true",
                   help="also write <stem>.entities.json / .entities.ttl (python -m scripts.entity_view makes "
                        "them later from any TTL)")
    a.add_argument("--mapper-url", default=None, help="local hybrid mapper (default from concept_mapping.json)")
    a.add_argument("--mapping-sources", default=None,
                   help="override concept_mapping.json sources_priority, e.g. 'trusted,bioportal' "
                        "(BioPortal needs BIOPORTAL_API_KEY in the environment)")
    a.add_argument("--no-docling", action="store_true")
    a.add_argument("--no-grobid", action="store_true")
    a.set_defaults(fn=cmd_init)
    for name, fn in (("next", cmd_next), ("run", cmd_run), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("--manifest", required=True)
        if name != "status":
            p.add_argument("--paper", default=None, help="only this paper stem (one worker per paper)")
        if name == "run":
            p.add_argument("--model", default=None, help="default: claude-code inside Claude Code")
        p.set_defaults(fn=fn)
    r = sub.add_parser("retry", help="reset a paper (all variants unless --variant)")
    r.add_argument("stem")
    r.add_argument("--manifest", required=True)
    r.add_argument("--variant", default=None)
    r.add_argument("--from-stage", choices=["all", "extract", "map", "kg_plan", "judge", "ttl"], default="ttl",
                   help="ttl: re-render from kept JSON; map: re-map and re-judge from the kept extraction; "
                        "kg_plan / judge: redo from there; extract/all: from scratch")
    r.set_defaults(fn=cmd_retry)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
