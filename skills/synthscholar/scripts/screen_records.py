#!/usr/bin/env python3
"""Title/abstract screening of PubMed search results, done by the host agent.

Sits between ``generate_search_strategy.py --fetch`` and ``fetch_ezproxy.py``:
a PubMed search returns hundreds or thousands of records, and only those that
plausibly meet the protocol's eligibility criteria are worth fetching in full.
The agent (or parallel subagents) judges each title/abstract; this script
splits the records into batches, checks every decision, and turns the result
into a ``corpus.json`` the rest of Mode 3 already understands, plus an audit
log of every decision and its reason.

    # 1. split the records into batches, with the criteria to judge them by
    python screen_records.py plan searches/<run>/results.jsonl \\
        --protocol protocol.json --workdir screening/

    # 2. for each screening/batch_NNN.json, write screening/decisions_NNN.json
    #    following screening/instructions.json -- one agent in sequence, or one
    #    subagent per batch in parallel; batches are independent
    python screen_records.py status --workdir screening/

    # 3. merge: corpus of the records kept, screening log, PRISMA counts
    python screen_records.py apply --workdir screening/ --corpus-out corpus.json \\
        --provenance search_provenance.json --model-name <your model id>

    # then full text, open access first:
    python fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs

``include`` and ``unsure`` records are both kept for full-text assessment (when
in doubt, keep: excluding is only for a record that clearly fails a criterion).
Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DECISIONS = ("include", "exclude", "unsure")
KEEP = ("include", "unsure")

RULES = """\
You are screening records for a systematic review at the TITLE/ABSTRACT stage.

For EVERY record in the batch, decide one of:
  include  the record plausibly meets every inclusion criterion and no
           exclusion criterion applies
  exclude  the record clearly fails at least one criterion. The reason must
           name the criterion it fails (e.g. "exclusion: animal study",
           "inclusion: no insulin-sensitivity outcome")
  unsure   the title/abstract does not say enough to decide (no abstract,
           ambiguous population, ...). These go on to full-text assessment.

Judge only from the record's own title and abstract, against the criteria
below. Do not use outside knowledge of the paper. When in doubt, choose
unsure, not exclude: a record wrongly excluded here is lost to the review,
while a record wrongly kept is caught at full text.

Answer with JSON only, covering every pmid in the batch exactly once:
{"decisions": [{"pmid": "...", "decision": "include|exclude|unsure", "reason": "..."}]}"""

CRITERIA_FIELDS = ("title", "objective", "pico_population", "pico_intervention",
                   "pico_comparison", "pico_outcome", "inclusion_criteria",
                   "exclusion_criteria", "date_range_start", "date_range_end")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_records(path: Path) -> list[dict]:
    """results.jsonl rows (generate_search_strategy.py --fetch), deduplicated by
    PMID in file order; rows without a PMID are skipped."""
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        pmid = str(row.get("pmid") or "").strip()
        if pmid and pmid not in seen:
            seen.add(pmid)
            out.append({**row, "pmid": pmid})     # one form everywhere (a JSON number is fine)
    return out


def _batch_name(i: int) -> str:
    return f"batch_{i:03d}.json"


def _decisions_name(i: int) -> str:
    return f"decisions_{i:03d}.json"


def _load_plan(workdir: Path) -> dict:
    path = workdir / "plan.json"
    if not path.exists():
        raise SystemExit(f"{path} not found -- run `screen_records.py plan` first")
    return json.loads(path.read_text(encoding="utf-8"))


def _check_batch(workdir: Path, i: int) -> tuple[str, list[dict]]:
    """('done' | 'missing' | 'invalid: <why>', decisions) for one batch."""
    batch = json.loads((workdir / _batch_name(i)).read_text(encoding="utf-8"))
    want = [str(r["pmid"]).strip() for r in batch["records"]]
    path = workdir / _decisions_name(i)
    if not path.exists():
        return "missing", []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"invalid: not JSON ({e})", []
    rows = data.get("decisions") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return 'invalid: expected {"decisions": [...]}', []
    got: dict[str, dict] = {}
    for d in rows:
        if not isinstance(d, dict):
            return f"invalid: each decision must be an object, got {d!r:.60}", []
        pmid = str(d.get("pmid") or "").strip()
        decision = str(d.get("decision") or "").strip().lower()
        reason = str(d.get("reason") or "").strip()
        if pmid not in want:
            return f"invalid: pmid {pmid!r} is not in this batch", []
        if pmid in got:
            return f"invalid: pmid {pmid} decided twice", []
        if decision not in DECISIONS:
            return f"invalid: pmid {pmid} decision {decision!r} (use {'/'.join(DECISIONS)})", []
        if decision == "exclude" and not reason:
            return f"invalid: pmid {pmid} excluded without a reason", []
        got[pmid] = {"pmid": pmid, "decision": decision, "reason": reason, "batch": i}
    missing = [p for p in want if p not in got]
    if missing:
        return f"invalid: no decision for {len(missing)} pmid(s), e.g. {missing[0]}", []
    return "done", [got[p] for p in want]


# ---------------------------------------------------------------- commands

def cmd_plan(args: argparse.Namespace) -> int:
    results, workdir = Path(args.results), Path(args.workdir)
    protocol = json.loads(Path(args.protocol).read_text(encoding="utf-8"))
    criteria = {k: protocol.get(k, "") for k in CRITERIA_FIELDS if protocol.get(k)}
    if not (criteria.get("inclusion_criteria") or criteria.get("exclusion_criteria")):
        raise SystemExit(f"{args.protocol} has no inclusion_criteria / exclusion_criteria "
                         "to screen against -- complete the protocol (Mode 1) first")
    records = _read_records(results)
    if not records:
        raise SystemExit(f"{results} has no records with a PMID")
    if workdir.exists() and any(workdir.glob("decisions_*.json")) and not args.force:
        raise SystemExit(f"{workdir} already has decisions; pass --force to re-plan "
                         "(this discards nothing, but batches may no longer match them)")
    workdir.mkdir(parents=True, exist_ok=True)

    size = max(1, args.batch_size)
    batches = [records[i:i + size] for i in range(0, len(records), size)]
    for n, recs in enumerate(batches, 1):
        (workdir / _batch_name(n)).write_text(json.dumps({
            "batch": n,
            "records": [{k: r.get(k, "") for k in
                         ("pmid", "title", "abstract", "year", "journal", "pub_types")}
                        for r in recs],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    (workdir / "instructions.json").write_text(json.dumps({
        "rules": RULES,
        "criteria": criteria,
        "for_each_batch": "read batch_NNN.json, write decisions_NNN.json (same NNN)",
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    (workdir / "plan.json").write_text(json.dumps({
        "results": str(results.resolve()),   # `apply` may run from another directory
        "protocol": str(Path(args.protocol).resolve()),
        "results_sha256": _sha256(results),
        "n_records": len(records),
        "batch_size": size,
        "n_batches": len(batches),
        "created": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    no_abstract = sum(1 for r in records if not str(r.get("abstract") or "").strip())
    print(f"{len(records)} records -> {len(batches)} batches of up to {size} in {workdir}"
          + (f" ({no_abstract} without an abstract)" if no_abstract else ""))
    print(f"Next: for each {workdir}/batch_NNN.json write decisions_NNN.json following "
          f"{workdir}/instructions.json, then `screen_records.py status --workdir {workdir}`.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    plan = _load_plan(workdir)
    counts = {"done": 0, "missing": 0, "invalid": 0}
    for i in range(1, plan["n_batches"] + 1):
        state, _ = _check_batch(workdir, i)
        key = state.split(":")[0]
        counts[key] += 1
        if key != "done":
            print(f"  {_batch_name(i)}: {state}")
    print(f"{plan['n_records']} records, {plan['n_batches']} batches: "
          f"{counts['done']} done, {counts['missing']} missing, {counts['invalid']} invalid")
    return 0 if counts["done"] == plan["n_batches"] else 1


def cmd_apply(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    plan = _load_plan(workdir)
    results = Path(plan["results"])
    if _sha256(results) != plan["results_sha256"]:
        raise SystemExit(f"{results} changed since `plan` -- re-run plan")
    decisions: list[dict] = []
    for i in range(1, plan["n_batches"] + 1):
        state, rows = _check_batch(workdir, i)
        if state != "done":
            raise SystemExit(f"{_batch_name(i)}: {state} -- see `screen_records.py status`")
        decisions += rows

    who = args.model_name or "unspecified"
    stamp = datetime.now(timezone.utc).isoformat()
    by_pmid = {d["pmid"]: d for d in decisions}
    with (workdir / "screening_log.jsonl").open("w", encoding="utf-8") as fh:
        for d in decisions:
            fh.write(json.dumps({**d, "stage": "title_abstract", "screened_by": who,
                                 "screened_at": stamp}, ensure_ascii=False) + "\n")

    items = []
    for r in _read_records(results):
        d = by_pmid[r["pmid"]]
        if d["decision"] not in KEEP:
            continue
        item = {
            "pmid": r["pmid"],
            "doi": r.get("doi", ""),
            "title": r.get("title", ""),
            "authors": r.get("authors", ""),
            "journal": r.get("journal", ""),
            "year": r.get("year", ""),
            "abstract": r.get("abstract", ""),
            "keywords": [],
            "mesh_terms": [t for t in str(r.get("mesh_terms") or "").split("; ") if t],
            "source": "PubMed",
            "full_text": "",
            # fetch_ezproxy.py fills full_text and its provenance; until then
            # the record can still be assessed on its abstract
            "_screening": {"stage": "title_abstract", "decision": d["decision"],
                           "reason": d["reason"], "screened_by": who},
        }
        item["_needs_metadata"] = [f for f in ("title", "authors", "year", "abstract")
                                   if not str(item.get(f) or "").strip()] + ["full_text"]
        items.append(item)

    Path(args.corpus_out).write_text(json.dumps({
        "corpus_version": 1,
        "generated_at": stamp,
        "provenance": "pubmed_search_screened",
        "discovery_performed_by": "agent (generate_search_strategy.py + screen_records.py)",
        "n_items": len(items),
        "items": items,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    n = len(decisions)
    excluded = sum(1 for d in decisions if d["decision"] == "exclude")
    unsure = sum(1 for d in decisions if d["decision"] == "unsure")
    if args.provenance:
        prov_path = Path(args.provenance)
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
        identified = int(prov.get("total_identified") or 0)
        prov["records_screened"] = n
        prov["records_excluded_title_abstract"] = excluded
        prov["reports_sought"] = len(items)
        note = (f"title/abstract screening of {n} records by {who} (screen_records.py): "
                f"{excluded} excluded, {len(items)} kept ({unsure} unsure)")
        prov["search_notes"] = "; ".join(x for x in (prov.get("search_notes"), note) if x)
        prov_path.write_text(json.dumps(prov, indent=2), encoding="utf-8")
        if identified and identified != n:
            print(f"WARNING: {identified} records identified but {n} screened -- the fetch did "
                  "not retrieve every record; the PRISMA flow will show the gap",
                  file=sys.stderr)
    print(f"{n} screened: {excluded} excluded, {len(items) - unsure} included, "
          f"{unsure} unsure -> {len(items)} in {args.corpus_out}")
    print(f"decisions -> {workdir / 'screening_log.jsonl'}"
          + (f"; counts -> {args.provenance}" if args.provenance else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="split search results into batches to screen")
    p.add_argument("results", help="results.jsonl from generate_search_strategy.py --fetch")
    p.add_argument("--protocol", required=True, help="protocol.json with the eligibility criteria")
    p.add_argument("--workdir", default="screening", help="where batches and decisions live")
    p.add_argument("--batch-size", type=int, default=25, metavar="N")
    p.add_argument("--force", action="store_true", help="re-plan over existing decisions")
    p.set_defaults(func=cmd_plan)

    s = sub.add_parser("status", help="which batches still need decisions")
    s.add_argument("--workdir", default="screening")
    s.set_defaults(func=cmd_status)

    a = sub.add_parser("apply", help="merge decisions into corpus.json + screening log")
    a.add_argument("--workdir", default="screening")
    a.add_argument("--corpus-out", default="corpus.json")
    a.add_argument("--provenance", default="", metavar="FILE",
                   help="search_provenance.json to update with the screening counts")
    a.add_argument("--model-name", default="", metavar="ID", help="who screened (provenance)")
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
