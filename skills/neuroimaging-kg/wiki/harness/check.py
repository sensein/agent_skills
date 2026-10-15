# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7", "pyshacl>=0.26"]
# ///
"""Score one WikiSkill rollout against gold computed here, independently of the agent's work.

Gold comes from the pinned source files (fetched into --cache, never the agent's directory) and from
rdflib walks over ReproNim's NIDM. Where a task requires the skill's validation to pass (or to fail),
the validator is re-run here on the agent's files.

Usage: uv run check.py <task_id> <task_dir> --cache <dir>    -> prints JSON {"task", "pass", "details"}
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef

HERE = Path(__file__).resolve().parent
SKILL = HERE.parents[1]
DEMO = SKILL / "examples" / "abide_one_site"
sys.path.insert(0, str(HERE))
from stage import DEFECTS  # noqa: E402

PROV = Namespace("http://www.w3.org/ns/prov#")
NIDM = Namespace("http://purl.org/nidash/nidm#")
NKG = Namespace("urn:neuroimaging-kg:")
CRYPTO = Namespace("http://id.loc.gov/vocabulary/preservation/cryptographicHashFunctions#")
NDAR_ID = URIRef("https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id")
HIPPO = URIRef("http://purl.obolibrary.org/obo/UBERON_0001954")
FS = URIRef("https://surfer.nmr.mgh.harvard.edu/")
FSL = URIRef("http://purl.org/nidash/fsl#")
MISSING = {"", "-9999", "-9999.0"}
SITE_ID = {"Caltech": "CALTECH", "SBL": "SBL", "OHSU": "OHSU", "Stanford": "STANFORD"}


def fetch(site: str, cache: Path) -> Path:
    d = cache / site
    if not (d / f"{site}_nidm.ttl").exists():
        subprocess.run(["uv", "run", "-q", str(DEMO / "fetch_inputs.py"), "--site", site, "--dest", str(d)],
                       check=True, stdout=subprocess.DEVNULL)
    return d


def rows(site: str, cache: Path) -> list[dict]:
    with open(fetch(site, cache) / "Phenotypic_V1_0b_preprocessed1.csv", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["SITE_ID"] == SITE_ID[site]]


def sid(x) -> str:
    return str(int(float(str(x).strip())))


def volumes(site: str, cache: Path, side: str | None) -> dict[str, dict[str, float]]:
    """{participant: {tool: hippocampal volume mm^3}} (one side, or left+right summed when side is None)."""
    d = fetch(site, cache)
    nidm = Graph().parse(d / f"{site}_nidm.ttl")
    cde = Graph()
    for t in ("fsl", "freesurfer", "ants"):
        cde.parse(d / f"{t}_cde.ttl")
    measures = {}
    for m in cde.subjects(NIDM.isAbout, HIPPO):
        if (m, NIDM.hasUnit, Literal("mm^3")) not in cde:
            continue
        lat = str(cde.value(m, NIDM.hasLaterality) or "").lower()
        if side is None or lat == side:
            measures[m] = lat
    tool_of = {a: t for a, t in nidm.subject_objects(NIDM.NIDM_0000164)}
    out: dict[str, dict[str, float]] = {}
    for act in set(nidm.subjects(PROV.qualifiedAssociation, None)):
        agents = {nidm.value(q, PROV.agent) for q in nidm.objects(act, PROV.qualifiedAssociation)}
        tools = [tool_of[a] for a in agents if a in tool_of]
        subj = [nidm.value(a, NDAR_ID) for a in agents if nidm.value(a, NDAR_ID) is not None]
        if not tools or not subj:
            continue
        tool = "FSL" if tools[0] == FSL else "FreeSurfer" if tools[0] == FS else str(tools[0])
        for ent in nidm.subjects(PROV.wasGeneratedBy, act):
            for m in measures:
                for v in nidm.objects(ent, m):
                    rec = out.setdefault(str(subj[0]), {})
                    rec[tool] = rec.get(tool, 0.0) + float(v)
    return out


def nidm_values(site: str, cache: Path, variables: set[str]) -> dict[str, dict[str, str]]:
    nidm = Graph().parse(fetch(site, cache) / f"{site}_nidm.ttl")
    var_of = {de: str(v) for de, v in nidm.subject_objects(NIDM.sourceVariable) if str(v) in variables}
    out: dict[str, dict[str, str]] = {}
    for act in set(nidm.subjects(PROV.qualifiedAssociation, None)):
        agents = [nidm.value(q, PROV.agent) for q in nidm.objects(act, PROV.qualifiedAssociation)]
        subj = [nidm.value(a, NDAR_ID) for a in agents if nidm.value(a, NDAR_ID) is not None]
        if not subj:
            continue
        for ent in nidm.subjects(PROV.wasGeneratedBy, act):
            for de, var in var_of.items():
                for v in nidm.objects(ent, de):
                    out.setdefault(str(subj[0]), {})[var] = str(v)
    return out


def answer(task: Path):
    p = task / "answer.json"
    return json.loads(p.read_text()) if p.exists() else None


def validate(task: Path, files: list[str], mutation: bool) -> int:
    cmd = ["uv", "run", "-q", str(SKILL / "scripts" / "validate_graph.py")]
    for f in files:
        cmd += ["--data", str(task / f)]
    if mutation:
        cmd.append("--mutation-test")
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def by_dx(rs: list[dict]) -> dict[str, int]:
    c = Counter(r["DX_GROUP"] for r in rs)
    return {k: c.get(k, 0) for k in ("1", "2")}


def ok_fiq(r: dict) -> bool:
    return r["FIQ"].strip() not in MISSING and float(r["FIQ"]) > 0


def ok_fd(r: dict, mx: float) -> bool:
    return r["func_mean_fd"].strip() not in MISSING and float(r["func_mean_fd"]) < mx


def norm_dict(d) -> dict:
    return {str(k): int(v) for k, v in (d or {}).items()}


def check(task_id: str, task: Path, cache: Path) -> dict:
    a = answer(task)
    det: dict = {"answer": a}
    if a is None:
        return {"pass": False, "details": {"error": "no answer.json"}}
    if task_id == "T1_sbl_fiq_hippocampus":
        fs = {p for p, t in volumes("SBL", cache, None).items() if "FreeSurfer" in t}
        gold = by_dx([r for r in rows("SBL", cache) if ok_fiq(r) and sid(r["SUB_ID"]) in fs])
        det["gold"] = gold
        return {"pass": norm_dict(a) == gold and (task / "graph").is_dir(), "details": det}
    if task_id == "T2_stanford_coverage":
        rs = rows("Stanford", cache)
        nv = nidm_values("Stanford", cache, {"AGE_AT_SCAN", "SEX", "DX_GROUP"})
        linked = [r for r in rs if sid(r["SUB_ID"]) in nv]
        conflicts = 0
        for r in linked:
            v = nv[sid(r["SUB_ID"])]
            bad = (abs(float(v.get("AGE_AT_SCAN", r["AGE_AT_SCAN"])) - float(r["AGE_AT_SCAN"])) > 0.01
                   or sid(v.get("SEX", r["SEX"])) != sid(r["SEX"]) or sid(v.get("DX_GROUP", r["DX_GROUP"])) != sid(r["DX_GROUP"]))
            conflicts += bad
        gold = {"phenotype": len(rs), "linked": len(linked), "conflicts": conflicts}
        det["gold"] = gold
        return {"pass": {k: int(a.get(k, -1)) for k in gold} == gold, "details": det}
    if task_id == "T3_caltech_derived_layer":
        tot = {p: t["FreeSurfer"] for p, t in volumes("Caltech", cache, None).items() if "FreeSurfer" in t}
        both = {p for p, t in volumes("Caltech", cache, "left").items() if "FreeSurfer" in t} & \
               {p for p, t in volumes("Caltech", cache, "right").items() if "FreeSurfer" in t}
        gold_tot = {p: tot[p] for p in both}
        det["gold"] = {"n": len(gold_tot), "mean_total_mm3": round(sum(gold_tot.values()) / len(gold_tot), 1)}
        csv_path = task / "derived" / "hippocampus_total.csv"
        csv_ok = False
        if csv_path.exists():
            with open(csv_path, newline="") as fh:
                got = {sid(r["src_subject_id"]): float(r["total_mm3"]) for r in csv.DictReader(fh)}
            csv_ok = set(got) == set(gold_tot) and all(abs(got[p] - gold_tot[p]) < 0.5 for p in got)
        det["csv_ok"] = csv_ok
        prov_ok = False
        if (task / "graph" / "provenance.ttl").exists() and csv_path.exists():
            g = Graph().parse(task / "graph" / "provenance.ttl")
            digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            outs = list(g.subjects(CRYPTO.sha256, Literal(digest)))
            acts = [act for o in outs for act in g.objects(o, PROV.wasGeneratedBy)]
            ai = any((ag, NKG.model, None) in g for act in acts for ag in g.objects(act, PROV.wasAssociatedWith))
            used_nidm = hashlib.sha256((fetch("Caltech", cache) / "Caltech_nidm.ttl").read_bytes()).hexdigest()
            uses = any((u, CRYPTO.sha256, Literal(used_nidm)) in g for act in acts for u in g.objects(act, PROV.used))
            prov_ok = bool(acts) and ai and uses
            det["prov"] = {"output_found": bool(outs), "activity": bool(acts), "ai_agent_with_model": ai, "used_site_nidm": uses}
        valid = validate(task, ["graph/provenance.ttl"], True) == 0
        det["validation_with_mutation"] = valid
        ans_ok = int(a.get("n", -1)) == det["gold"]["n"] and abs(float(a.get("mean_total_mm3", -1)) - det["gold"]["mean_total_mm3"]) <= 0.1
        return {"pass": csv_ok and prov_ok and valid and ans_ok, "details": det}
    if task_id == "T4_caltech_add_claim":
        f = task / "graph" / "claims_dimartino.ttl"
        if not f.exists():
            return {"pass": False, "details": {"error": "no graph/claims_dimartino.ttl"}}
        g = Graph().parse(f)
        quote = "both were detected, though hypoconnectivity dominated, particularly for cortico-cortical and interhemispheric functional connectivity"
        claims = list(g.subjects(None, NKG.Claim))
        good = any(
            (c, NKG.claimType, Literal("associational")) in g
            and (c, NKG.usesDataset, URIRef("urn:neuroimaging-kg:abide:dataset/abide_i")) in g
            and any(str(q) == quote for e in g.objects(c, NKG.evidence) for q in g.objects(e, NKG.quote))
            for c in claims)
        valid = validate(task, ["graph/claims_dimartino.ttl"], False) == 0
        papers = {str(x.get("paper", "")) for x in a} if isinstance(a, list) else set()
        ans_ok = isinstance(a, list) and len(a) == 2 and any("mp.2013.78" in p for p in papers) and any("neuroimage.2016.10.045" in p for p in papers)
        det.update({"claim_shape_ok": good, "validates": valid, "answer_ok": ans_ok})
        return {"pass": good and valid and ans_ok, "details": det}
    if task_id == "T5_messy_join":
        d = DEFECTS["caltech_local_table"]
        n = {"matched": 38 - len(d["dropped"]), "only_local": set(d["extra"]), "only_external": set(d["dropped"]),
             "age_conflicts": set(d["age_plus_5"])}
        got = {"matched": int(a.get("matched", -1)), "only_local": {sid(x) for x in a.get("only_local", [])},
               "only_external": {sid(x) for x in a.get("only_external", [])},
               "age_conflicts": {sid(x) for x in a.get("age_conflicts", [])}}
        det["gold"] = {k: sorted(v) if isinstance(v, set) else v for k, v in n.items()}
        return {"pass": got == n, "details": det}
    if task_id == "T6_egress_gate":
        f = task / "graph" / "egress.ttl"
        if not f.exists():
            return {"pass": False, "details": {"error": "no graph/egress.ttl"}}
        g = Graph().parse(f)
        scoped = any(str(o) in ("study-only", "named-uses") for o in g.objects(None, NKG.consentScope))
        promoted = (None, NKG.promotionStatus, Literal("promoted")) in g
        blocked_by_validation = validate(task, ["graph/egress.ttl"], False) != 0
        ans_ok = a.get("promotion_allowed") is False
        det.update({"consent_scope_recorded": scoped, "promotion_recorded": promoted,
                    "validation_blocks": blocked_by_validation, "answer_ok": ans_ok})
        return {"pass": scoped and promoted and blocked_by_validation and ans_ok, "details": det}
    if task_id == "V1_ohsu_motion_and_volumes":
        fs = {p for p, t in volumes("OHSU", cache, None).items() if "FreeSurfer" in t}
        rs = [r for r in rows("OHSU", cache) if ok_fd(r, 0.15)]
        gold = {"fd": by_dx(rs), "fd_fiq_fs": by_dx([r for r in rs if ok_fiq(r) and sid(r["SUB_ID"]) in fs])}
        det["gold"] = gold
        got = {"fd": norm_dict(a.get("fd")), "fd_fiq_fs": norm_dict(a.get("fd_fiq_fs"))}
        return {"pass": got == gold, "details": det}
    if task_id == "V2_caltech_tool_disagreement":
        left = volumes("Caltech", cache, "left")
        both = {p: t for p, t in left.items() if "FSL" in t and "FreeSurfer" in t}
        ids = sorted(p for p, t in both.items() if abs(t["FSL"] - t["FreeSurfer"]) / ((t["FSL"] + t["FreeSurfer"]) / 2) > 0.25)
        gold = {"n_with_both": len(both), "ids": ids}
        det["gold"] = gold
        got = {"n_with_both": int(a.get("n_with_both", -1)), "ids": sorted(sid(x) for x in a.get("ids", []))}
        return {"pass": got == gold, "details": det}
    if task_id == "V3_malformed_table":
        d = DEFECTS["caltech_messy_table"]
        gold = {"rows": 39, "duplicate_ids": [d["duplicate"]], "matched": 37}
        det["gold"] = gold | {"age_conflicts": "must include " + str(d["age_plus_10"]) + ", may include the duplicate"}
        conf = {sid(x) for x in a.get("age_conflicts", [])}
        ok = (int(a.get("rows", -1)) == 39 and sorted(sid(x) for x in a.get("duplicate_ids", [])) == gold["duplicate_ids"]
              and int(a.get("matched", -1)) == 37 and set(d["age_plus_10"]) <= conf <= set(d["age_plus_10"]) | {d["duplicate"]})
        return {"pass": ok, "details": det}
    raise SystemExit(f"unknown task {task_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task_id")
    ap.add_argument("task_dir")
    ap.add_argument("--cache", required=True)
    a = ap.parse_args()
    res = check(a.task_id, Path(a.task_dir), Path(a.cache))
    print(json.dumps({"task": a.task_id} | res, default=str))


if __name__ == "__main__":
    main()
