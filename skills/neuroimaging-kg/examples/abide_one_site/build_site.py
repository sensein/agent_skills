# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Build the knowledge graph for one ABIDE I site with the neuroimaging-kg skill's steps.

Inputs come from fetch_inputs.py (pinned, hash-checked). Outputs go to graph/:
  phenotype.ttl       participants + phenotype values, written by PyNIDM csv2nidm (NIDM)
  identity_links.ttl  reviewable links: our phenotype rows <-> ReproNim's site NIDM participants
  cohort.ttl          participants passing a head-motion rule, with the rule and its provenance
  dataset_claims.ttl  the ABIDE I dataset record (consent scope) and one scoped published claim
  provenance.ttl      PROV record of every step above (inputs by SHA-256, software, parameters, agent)
plus join_report.json. ReproNim's site NIDM and tool CDE files are used as they are.

Usage: uv run build_site.py --site Caltech --data data --out graph --agent "Your Name"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import join_identity  # noqa: E402
import record_activity  # noqa: E402

NKG = Namespace("urn:neuroimaging-kg:")
PROV = Namespace("http://www.w3.org/ns/prov#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
NDAR_ID = URIRef("https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id")
BASE = "urn:neuroimaging-kg:abide:"
ABIDE_I = URIRef(BASE + "dataset/abide_i")
MISSING = {"", "-9999", "-9999.0", "nan", "NaN"}  # ABIDE codes missing values as -9999


def step(msg: str) -> float:
    print(f"\n== {msg}", flush=True)
    return time.time()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", required=True)
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="graph")
    ap.add_argument("--agent", default="Demo user")
    ap.add_argument("--fd-max", type=float, default=0.2, help="mean framewise displacement threshold (mm)")
    a = ap.parse_args()
    data, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pins = json.loads((HERE / "pins.json").read_text())
    site_id = pins["site_nidm"][a.site]["site_id"]
    prov = Graph()

    # 1. Scope the table to one site, keeping the columns ReproNim's term map covers.
    t = step(f"1/6 subset the ABIDE phenotype table to site {site_id}")
    terms = json.loads((data / "abide_terms.json").read_text())
    mapped = [re.search(r"variable='([^']+)'", k).group(1) for k in terms]
    with open(data / "Phenotypic_V1_0b_preprocessed1.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["SITE_ID"] == site_id]
    cols = [c for c in mapped if c in rows[0]]
    subset = out / f"{a.site}_phenotype_mapped.csv"
    with open(subset, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    motion = out / f"{a.site}_motion.csv"
    with open(motion, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["SUB_ID", "DX_GROUP", "func_mean_fd"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    record_activity.record("subset-site", f"Select {site_id} rows and the {len(cols)} term-mapped columns",
                           used=[str(data / "Phenotypic_V1_0b_preprocessed1.csv"), str(data / "abide_terms.json")],
                           generated=[str(subset), str(motion)], software=["python==" + sys.version.split()[0]],
                           params={"SITE_ID": site_id}, agent=a.agent, base=BASE, graph=prov)
    print(f"   {len(rows)} participants, {len(cols)} mapped columns ({time.time() - t:.1f} s)")

    # 2. Dataset layer: PyNIDM csv2nidm turns the table into NIDM, using shared concept IRIs.
    t = step("2/6 convert phenotype rows to NIDM with PyNIDM csv2nidm (non-interactive)")
    pheno_ttl = out / "phenotype.ttl"
    cmd = ["uvx", "--python", "3.12", "--from", pins["pynidm"], "csv2nidm", "-csv", subset.name,
           "-json_map", str((data / "abide_terms.json").resolve()), "-no_concepts", "-out", pheno_ttl.name, "-log", "."]
    p = subprocess.run(cmd, cwd=out, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"csv2nidm failed:\n{p.stderr[-2000:]}")
    record_activity.record("csv2nidm", "Convert phenotype rows to NIDM (csv2nidm -json_map ... -no_concepts)",
                           used=[str(subset), str(data / "abide_terms.json")], generated=[str(pheno_ttl)],
                           software=[pins["pynidm"]], agent=a.agent, base=BASE, graph=prov)
    pheno = Graph().parse(pheno_ttl)
    print(f"   {len(pheno)} triples ({time.time() - t:.1f} s)")

    # 3. Join across graphs: our rows <-> ReproNim's NIDM participants, as reviewable links.
    t = step("3/6 join our participants to ReproNim's site NIDM (reviewable identity links)")
    site_nidm = data / f"{a.site}_nidm.ttl"
    ext = Graph().parse(site_nidm)
    links, report = join_identity.join(
        rows, "SUB_ID", ext, normalize="int",
        checks={"AGE_AT_SCAN": "about:http://uri.interlex.org/ilx_0100400",
                "SEX": "about:http://uri.interlex.org/base/ilx_0101292",
                "DX_GROUP": "var:DX_GROUP"},
        reviewer=a.agent, base=BASE, numeric_tolerance=0.01)
    links.serialize(out / "identity_links.ttl", format="turtle")
    (out / "join_report.json").write_text(json.dumps(report, indent=1))
    record_activity.record("identity-join-record", "Identity join SUB_ID -> ndar:src_subject_id with age/sex/diagnosis checks",
                           used=[str(subset), str(site_nidm)], generated=[str(out / "identity_links.ttl")],
                           software=["neuroimaging-kg/join_identity.py==0.1"], params={"normalize": "int", "age_tolerance": "0.01"},
                           agent=a.agent, base=BASE, graph=prov)
    print(f"   matched {report['matched']}, only in our table {len(report['only_local'])}, "
          f"only in ReproNim {len(report['only_external'])}, conflicts {len(report['conflicts'])} ({time.time() - t:.1f} s)")

    # 4. An analysis decision, recorded: the head-motion cohort.
    t = step(f"4/6 define the head-motion cohort (mean FD < {a.fd_max} mm) and record how")
    agents = {str(o): s for s, o in pheno.subject_objects(NDAR_ID)}
    cohort = URIRef(f"{BASE}cohort/{a.site}_meanfd_lt_{a.fd_max}")
    cg = Graph()
    cg.add((cohort, RDF.type, NKG.Cohort))
    cg.add((cohort, RDF.type, PROV.Collection))
    cg.add((cohort, RDFS.label, Literal(f"{a.site}: mean FD < {a.fd_max} mm")))
    cg.add((cohort, NKG.rule, Literal(f"func_mean_fd < {a.fd_max} (mm); rows with missing (-9999) mean FD excluded")))
    kept = 0
    for r in rows:
        fd = r["func_mean_fd"].strip()
        if fd in MISSING or not math.isfinite(float(fd)) or float(fd) >= a.fd_max:
            continue
        member = agents.get(str(int(float(r["SUB_ID"]))))
        if member is not None:
            cg.add((cohort, PROV.hadMember, member))
            kept += 1
    cg.add((cohort, NKG.n, Literal(kept)))
    cg.serialize(out / "cohort.ttl", format="turtle")
    record_activity.record("motion-cohort", f"Keep participants with mean FD < {a.fd_max} mm",
                           used=[str(motion), str(pheno_ttl)], generated=[str(cohort)],
                           software=["neuroimaging-kg/build_site.py==0.1"], params={"mean_fd_max_mm": str(a.fd_max)},
                           agent=a.agent, base=BASE, graph=prov)
    prov.add((cohort, PROV.wasGeneratedBy, URIRef(f"{BASE}activity/motion-cohort")))
    print(f"   {kept} of {len(rows)} participants kept ({time.time() - t:.1f} s)")

    # 5. Dataset record with consent scope, and one scoped published claim with its evidence.
    t = step("5/6 add the dataset record (consent scope) and a scoped claim from Abraham et al. 2017")
    dc = Graph()
    dc.bind("nkg", NKG)
    dc.add((ABIDE_I, RDF.type, DCAT.Dataset))
    dc.add((ABIDE_I, RDFS.label, Literal("ABIDE I (Autism Brain Imaging Data Exchange), Preprocessed Connectomes Project release")))
    dc.add((ABIDE_I, DCAT.landingPage, URIRef("http://preprocessed-connectomes-project.org/abide/")))
    prof = URIRef(BASE + "dataset/abide_i/sensitivity")
    dc.add((ABIDE_I, NKG.sensitivity, prof))
    dc.add((prof, RDF.type, NKG.SensitivityProfile))
    dc.add((prof, NKG.consentScope, Literal("broad-sharing")))
    dc.add((prof, RDFS.comment, Literal("Public, de-identified release; held in the shared lake, linked rather than copied.")))
    dc.add((ABIDE_I, NKG.knowledgeScope, Literal("lake")))
    paper = URIRef("https://doi.org/10.1016/j.neuroimage.2016.10.045")
    claim = URIRef(BASE + "claim/abraham2017_accuracy")
    span = URIRef(BASE + "claim/abraham2017_accuracy/evidence")
    dc.add((paper, RDFS.label, Literal("Abraham et al. 2017, NeuroImage: Deriving reproducible biomarkers from multi-site resting-state data")))
    dc.add((claim, RDF.type, NKG.Claim))
    dc.add((claim, NKG.claimType, Literal("predictive")))
    dc.add((claim, NKG.population, Literal("ABIDE I, 871 of 1,112 participants after quality control, all sites")))
    dc.add((claim, NKG.measure, Literal("accuracy classifying autistic vs non-autistic participants from resting-state connectivity")))
    dc.add((claim, NKG.evaluation, Literal("whole sites held out (leave-site-out) and other schemes")))
    dc.add((claim, NKG.value, Literal("0.67")))
    dc.add((claim, NKG.usesDataset, ABIDE_I))
    dc.add((claim, DCT.source, paper))
    dc.add((claim, NKG.evidence, span))
    dc.add((span, RDF.type, NKG.EvidenceSpan))
    dc.add((span, NKG.quote, Literal("Good choices of methods for the various steps of the pipeline lead to 67% prediction accuracy on the full ABIDE data")))
    dc.add((span, NKG.source, URIRef("https://arxiv.org/abs/1611.06066v1")))
    dc.add((span, NKG.locator, Literal("Abstract (arXiv v1 text; quote checked verbatim)")))
    dc.serialize(out / "dataset_claims.ttl", format="turtle")
    record_activity.record("author-claim", "Record one published claim with scope and a verbatim evidence quote",
                           used=["https://arxiv.org/abs/1611.06066v1"], generated=[str(claim)],
                           software=["neuroimaging-kg/build_site.py==0.1"], agent=a.agent, base=BASE, graph=prov)
    print(f"   dataset record + 1 claim ({time.time() - t:.1f} s)")

    t = step("6/6 write the provenance graph")
    prov.serialize(out / "provenance.ttl", format="turtle")
    print(f"   {len(prov)} triples ({time.time() - t:.1f} s)")


if __name__ == "__main__":
    main()
