# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Compute gold answers for the demo's competency questions without using the SPARQL store.

Q1, Q2 and Q4 come straight from the source files (the phenotype table, the raw NIDM text, a file
hash), so they test the graph against the data it was built from. Q3 needs brain volumes that
only exist in ReproNim's NIDM, so its gold is computed by walking the RDF triples in Python
(rdflib) — an independent method over the same graph, labelled as such. Q5 is the claim the
build step recorded. Q6 is descriptive and has no gold.

Usage: uv run gold.py --site Caltech --data data --graph graph --out graph/gold.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef

PROV = Namespace("http://www.w3.org/ns/prov#")
NIDM = Namespace("http://purl.org/nidash/nidm#")
NDAR_ID = URIRef("https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id")
HIPPOCAMPUS = URIRef("http://purl.obolibrary.org/obo/UBERON_0001954")
FREESURFER = URIRef("https://surfer.nmr.mgh.harvard.edu/")
MISSING = {"", "-9999", "-9999.0"}


def rows_for(site_id: str, data: Path) -> list[dict]:
    with open(data / "Phenotypic_V1_0b_preprocessed1.csv", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["SITE_ID"] == site_id]


def ids_with_freesurfer_hippocampus(data: Path, site: str) -> set[str]:
    nidm = Graph().parse(data / f"{site}_nidm.ttl")
    cde = Graph().parse(data / "freesurfer_cde.ttl")
    measures = {m for m in cde.subjects(NIDM.isAbout, HIPPOCAMPUS) if (m, NIDM.hasUnit, Literal("mm^3")) in cde}
    fs_agents = set(nidm.subjects(NIDM.NIDM_0000164, FREESURFER))
    ids = set()
    for act in set(nidm.subjects(PROV.qualifiedAssociation, None)):
        agents = {nidm.value(a, PROV.agent) for a in nidm.objects(act, PROV.qualifiedAssociation)}
        if not agents & fs_agents:
            continue
        subjects = [ag for ag in agents if nidm.value(ag, NDAR_ID) is not None]
        has_volume = any((ent, m, None) in nidm for ent in nidm.subjects(PROV.wasGeneratedBy, act) for m in measures)
        if has_volume:
            ids.update(str(nidm.value(s, NDAR_ID)) for s in subjects)
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", required=True)
    ap.add_argument("--data", default="data")
    ap.add_argument("--graph", default="graph")
    ap.add_argument("--fd-max", type=float, default=0.2)
    ap.add_argument("--out", default="graph/gold.json")
    a = ap.parse_args()
    data, gdir = Path(a.data), Path(a.graph)
    pins = json.loads((Path(__file__).resolve().parent / "pins.json").read_text())
    rows = rows_for(pins["site_nidm"][a.site]["site_id"], data)
    raw_ids = set(re.findall(r'src_subject_id "(\d+)"', (data / f"{a.site}_nidm.ttl").read_text()))
    sub = lambda r: str(int(float(r["SUB_ID"])))  # noqa: E731

    gold = {}
    gold["Q1_participants"] = [{"in_phenotype": str(len(rows)),
                                "linked_to_repronim": str(sum(sub(r) in raw_ids for r in rows))}]
    kept = Counter(r["DX_GROUP"] for r in rows
                   if r["func_mean_fd"].strip() not in MISSING and float(r["func_mean_fd"]) < a.fd_max)
    gold["Q2_cohort_by_diagnosis"] = [{"dx_group": k, "n": str(v)} for k, v in sorted(kept.items())]
    fs_ids = ids_with_freesurfer_hippocampus(data, a.site)
    both = Counter(r["DX_GROUP"] for r in rows
                   if r["FIQ"].strip() not in MISSING and float(r["FIQ"]) > 0 and sub(r) in fs_ids)
    gold["Q3_fiq_and_freesurfer_hippocampus"] = [{"dx_group": k, "n": str(v)} for k, v in sorted(both.items())]
    motion = gdir / f"{a.site}_motion.csv"
    gold["Q4_cohort_provenance"] = [{
        "step": f"Keep participants with mean FD < {a.fd_max} mm", "software": "neuroimaging-kg/build_site.py",
        "version": "0.1", "parameter": "mean_fd_max_mm", "value": str(a.fd_max), "input_file": motion.name,
        "input_sha256": hashlib.sha256(motion.read_bytes()).hexdigest()}]
    gold["Q5_claims_using_dataset"] = [{
        "claim_type": "predictive", "population": "ABIDE I, 871 of 1,112 participants after quality control, all sites",
        "value": "0.67",
        "quote": "Good choices of methods for the various steps of the pipeline lead to 67% prediction accuracy on the full ABIDE data",
        "paper": "https://doi.org/10.1016/j.neuroimage.2016.10.045", "consent_scope": "broad-sharing"}]
    gold["_method"] = {"Q1,Q2,Q4": "source files (CSV, raw NIDM text, file hash)",
                       "Q3": "rdflib triple walk over ReproNim NIDM + FreeSurfer CDE (independent of the SPARQL store)",
                       "Q5": "claim recorded by build_site.py", "Q6": "descriptive, no gold"}
    Path(a.out).write_text(json.dumps(gold, indent=1))
    print(f"gold answers for {len(gold) - 1} questions -> {a.out}")


if __name__ == "__main__":
    main()
