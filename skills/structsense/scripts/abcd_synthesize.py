"""Cross-paper synthesis over verified ABCD/HBCD extractions.

Answers the questions a literature review actually asks:

  * **Where is there consensus across constructs?** Group findings by Cognitive
    Atlas construct, tally the direction each paper reports, and report the
    majority direction with an agreement score.
  * **Where is there divergence?** A construct is divergent when papers report
    *opposing* directions (positive and negative both present), not merely when
    effect sizes differ. Disagreement about sign is the interesting case.
  * **Are some variables consistently mediators/moderators?** For every variable,
    tally the roles it plays across papers. A variable used as a mediator in five
    of six papers is a stable mediator; one split three-three is contested, and
    that is reported as contested rather than resolved by majority vote.

Counting is by PAPER, never by finding. One paper reporting the same association
in six models must not outvote five other papers — that would turn verbosity into
evidence.

Every aggregate carries `evidence[]` pointing back to paper id, section and the
verified quote, so a reader can check a synthesis claim without re-reading the
corpus.

    python -m scripts.abcd_synthesize ./out/*_abcd.json --out ./out/abcd_synthesis
    python -m scripts.abcd_synthesize ./out --min-papers 3
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
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from scripts import abcd_export

# A construct/variable needs this many distinct papers before we call anything a
# consensus. Below it, we report the observation without a verdict.
DEFAULT_MIN_PAPERS = 2
# Share of papers that must agree for "consistent"; below it, "contested".
AGREEMENT_THRESHOLD = 0.70

OPPOSING = ({"positive"}, {"negative"})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ev(paper_id: str, item: dict, **extra) -> dict:
    ev = item.get("evidence") or {}
    return {
        "paper_id": paper_id,
        "section": ev.get("section"),
        "page": ev.get("page"),
        "quote": ev.get("quote"),
        "used_context": ev.get("used_context"),
        "char_start": ev.get("start"),
        "char_end": ev.get("end"),
        **extra,
    }


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #

def synthesize(docs: List[dict], *, min_papers: int = DEFAULT_MIN_PAPERS,
               agreement_threshold: float = AGREEMENT_THRESHOLD) -> dict:
    """Build the synthesis document from per-paper extraction results."""
    papers: List[dict] = []

    # construct -> direction -> set of paper ids   (paper-level counting)
    c_dir: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    c_label: Dict[str, str] = {}
    c_ev: Dict[str, List[dict]] = defaultdict(list)

    # variable -> role -> set of paper ids
    v_role: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    v_ev: Dict[str, List[dict]] = defaultdict(list)
    v_meta: Dict[str, dict] = {}

    # co-occurrence: which papers pair a variable with a construct
    pair: Dict[Tuple[str, str], set] = defaultdict(set)

    for doc in docs:
        pid = str(doc.get("paper_id") or "?")
        meta = doc.get("source_metadata") or {}
        papers.append({
            "paper_id": pid,
            "title": meta.get("paper_title"),
            "source_path": meta.get("source_path"),
            "doi": meta.get("doi"),
            "study": meta.get("study"),
            "data_release": meta.get("data_release"),
            "variable_count": len(doc.get("variables") or []),
            "finding_count": len(doc.get("findings") or []),
            "rejected_count": len(doc.get("rejected") or []),
        })

        # -- variables: roles come from both the variable list and the models --
        for v in doc.get("variables") or []:
            name = str(v.get("name") or "").strip()
            if not name:
                continue
            role = str(v.get("role") or "unspecified").lower()
            v_role[name][role].add(pid)
            v_ev[name].append(_ev(pid, v, role=role,
                                  dictionary_status=v.get("dictionary_status")))
            v_meta.setdefault(name, {
                "dictionary_status": v.get("dictionary_status"),
                "dictionary_match": v.get("dictionary_match"),
            })

        for m in doc.get("models") or []:
            for key, role in (("predictors", "predictor"), ("outcomes", "outcome"),
                              ("mediators", "mediator"), ("moderators", "moderator"),
                              ("covariates", "covariate")):
                for name in m.get(key) or []:
                    n = str(name).strip()
                    if n:
                        v_role[n][role].add(pid)
                        v_ev[n].append(_ev(pid, m, role=role, from_model=True))

        # -- findings: direction per construct --
        for f in doc.get("findings") or []:
            cid = f.get("construct_id") or f"unmapped:{(f.get('construct') or '?').lower()}"
            label = f.get("construct_label") or f.get("construct") or "(unmapped)"
            c_label.setdefault(cid, label)
            direction = str(f.get("direction") or "unspecified").lower()
            c_dir[cid][direction].add(pid)
            c_ev[cid].append(_ev(pid, f, direction=direction,
                                 statement=f.get("statement"),
                                 role=f.get("role")))
            for name in f.get("variables") or []:
                if str(name).strip():
                    pair[(str(name).strip(), cid)].add(pid)
            role = str(f.get("role") or "").lower()
            if role in ("mediator", "moderator"):
                for name in f.get("variables") or []:
                    n = str(name).strip()
                    if n:
                        v_role[n][role].add(pid)
                        v_ev[n].append(_ev(pid, f, role=role, from_finding=True))

    constructs = [
        _construct_row(cid, c_label.get(cid, cid), dirs, c_ev[cid],
                       min_papers, agreement_threshold)
        for cid, dirs in c_dir.items()
    ]
    constructs.sort(key=lambda r: (-r["paper_count"], r["construct_label"] or ""))

    variables = [
        _variable_row(name, roles, v_ev[name], v_meta.get(name, {}),
                      min_papers, agreement_threshold)
        for name, roles in v_role.items()
    ]
    variables.sort(key=lambda r: (-r["paper_count"], r["variable"]))

    links = [
        {"variable": var, "construct_id": cid,
         "construct_label": c_label.get(cid, cid),
         "paper_count": len(pids), "papers": sorted(pids)}
        for (var, cid), pids in pair.items()
    ]
    links.sort(key=lambda r: -r["paper_count"])

    return {
        "synthesis_id": f"abcd-{len(papers)}papers",
        "papers": papers,
        "constructs": constructs,
        "variables": variables,
        "variable_construct_links": links,
        "totals": {
            "papers": len(papers),
            "variable_uses": sum(p["variable_count"] for p in papers),
            "findings": sum(p["finding_count"] for p in papers),
            "distinct_variables": len(variables),
            "distinct_constructs": len(constructs),
            "consensus_constructs": sum(1 for c in constructs
                                        if c["verdict"] == "consensus"),
            "divergent_constructs": sum(1 for c in constructs
                                        if c["verdict"] == "divergent"),
            "consistent_mediators": sum(
                1 for v in variables
                if v["verdict"] == "consistent_role" and v["dominant_role"] == "mediator"),
            "consistent_moderators": sum(
                1 for v in variables
                if v["verdict"] == "consistent_role" and v["dominant_role"] == "moderator"),
            "contested_roles": sum(1 for v in variables if v["verdict"] == "contested_role"),
        },
        "method": {
            "counting_unit": "paper",
            "min_papers_for_verdict": min_papers,
            "agreement_threshold": agreement_threshold,
            "divergence_rule": "opposing directions (positive and negative) both present",
            "note": ("Findings are counted once per paper per construct, so a paper "
                     "reporting many models cannot outweigh other papers."),
        },
        "provenance": {
            "run_at": _now(),
            "inputs": [p["source_path"] or p["paper_id"] for p in papers],
        },
    }


def _construct_row(cid: str, label: str, dirs: Dict[str, set],
                   evidence: List[dict], min_papers: int, thresh: float) -> dict:
    counts = {d: len(pids) for d, pids in dirs.items()}
    all_papers = set().union(*dirs.values()) if dirs else set()
    n = len(all_papers)
    substantive = {d: c for d, c in counts.items() if d in ("positive", "negative", "null")}
    majority, maj_n = (max(substantive.items(), key=lambda kv: kv[1])
                       if substantive else (None, 0))
    agreement = (maj_n / n) if n else 0.0
    has_pos = counts.get("positive", 0) > 0
    has_neg = counts.get("negative", 0) > 0

    if n < min_papers:
        verdict = "insufficient_papers"
    elif has_pos and has_neg:
        verdict = "divergent"
    elif majority and agreement >= thresh:
        verdict = "consensus"
    else:
        verdict = "mixed"

    return {
        "construct_id": None if cid.startswith("unmapped:") else cid,
        "construct_label": label,
        "construct_mapped": not cid.startswith("unmapped:"),
        "paper_count": n,
        "papers": sorted(all_papers),
        "directions": counts,
        "majority_direction": majority,
        "agreement": round(agreement, 3),
        "verdict": verdict,
        "evidence": evidence,
    }


def _variable_row(name: str, roles: Dict[str, set], evidence: List[dict],
                  meta: dict, min_papers: int, thresh: float) -> dict:
    counts = {r: len(pids) for r, pids in roles.items()}
    all_papers = set().union(*roles.values()) if roles else set()
    n = len(all_papers)
    # "unspecified" is not a role claim — don't let it win a majority.
    claimed = {r: c for r, c in counts.items() if r != "unspecified"}
    dominant, dom_n = (max(claimed.items(), key=lambda kv: kv[1])
                       if claimed else (None, 0))
    consistency = (dom_n / n) if n else 0.0

    if n < min_papers:
        verdict = "insufficient_papers"
    elif dominant and consistency >= thresh:
        verdict = "consistent_role"
    elif len(claimed) > 1:
        verdict = "contested_role"
    else:
        verdict = "mixed"

    return {
        "variable": name,
        "paper_count": n,
        "papers": sorted(all_papers),
        "roles": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "dominant_role": dominant,
        "role_consistency": round(consistency, 3),
        "verdict": verdict,
        "dictionary_status": meta.get("dictionary_status"),
        "dictionary_match": meta.get("dictionary_match"),
        "evidence": evidence,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_inputs(paths: List[Path]) -> List[dict]:
    files: List[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*_abcd.json")))
        else:
            files.append(p)
    docs = []
    for f in files:
        try:
            doc = json.loads(f.read_text())
        except Exception as exc:
            print(f"skipping {f}: {exc}", file=sys.stderr)
            continue
        if "variables" not in doc and "findings" not in doc:
            print(f"skipping {f}: not an ABCD extraction", file=sys.stderr)
            continue
        docs.append(doc)
    return docs


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="*_abcd.json files, or directories containing them")
    ap.add_argument("--out", type=Path, default=Path("abcd_synthesis"),
                    help="output stem (default: ./abcd_synthesis)")
    ap.add_argument("--formats", default="json,md,ttl")
    ap.add_argument("--min-papers", type=int, default=DEFAULT_MIN_PAPERS)
    ap.add_argument("--agreement-threshold", type=float, default=AGREEMENT_THRESHOLD)
    a = ap.parse_args(argv)

    docs = _load_inputs(a.inputs)
    if not docs:
        print("no ABCD extractions found", file=sys.stderr)
        return 1

    syn = synthesize(docs, min_papers=a.min_papers,
                     agreement_threshold=a.agreement_threshold)
    formats = [f.strip() for f in a.formats.split(",") if f.strip()]
    written = abcd_export.write_all(syn, a.out, kind="synthesis", formats=formats)
    t = syn["totals"]
    print(f"{t['papers']} papers | {t['distinct_constructs']} constructs "
          f"({t['consensus_constructs']} consensus, {t['divergent_constructs']} divergent) "
          f"| {t['distinct_variables']} variables "
          f"({t['consistent_mediators']} consistent mediators, "
          f"{t['consistent_moderators']} moderators, {t['contested_roles']} contested)")
    for fmt, path in written.items():
        print(f"  {fmt}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
