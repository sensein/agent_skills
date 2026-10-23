# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Extract brain-structure measures from NIDM files into a flat table (one row per value).

ReproNim's NIDM files (written by segstats_jsonld, ants_seg_to_nidm, fsl_seg_to_nidm) keep values and
definitions apart: the site file holds `entity prov:wasGeneratedBy activity ; <measure> value`, the
activity's qualified associations name the participant (ndar:src_subject_id) and the tool agent
(nidm:NIDM_0000164 <tool namespace>), and only the tool's CDE file says what <measure> is —
nidm:isAbout (region), nidm:hasLaterality, nidm:hasUnit, rdfs:label. This joins the two.

Select volumes by unit ("mm^3"): ANTs gives surface area the volume datum type, and FreeSurfer tags
voxel counts with the same region.

Usage:
  uv run scripts/nidm_volumes.py --nidm site_nidm.ttl --cde freesurfer_cde.ttl --cde fsl_cde.ttl \
      --region http://purl.obolibrary.org/obo/UBERON_0001954 --unit mm^3 --out hippocampus.csv
"""
from __future__ import annotations

import argparse
import csv
import sys

from rdflib import Graph, Literal, Namespace, URIRef

PROV = Namespace("http://www.w3.org/ns/prov#")
NIDM = Namespace("http://purl.org/nidash/nidm#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
NDAR_ID = URIRef("https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id")
TOOLS = {"https://surfer.nmr.mgh.harvard.edu/": "FreeSurfer", "http://purl.org/nidash/fsl#": "FSL",
         "http://stnava.github.io/ANTs/": "ANTs"}
FIELDS = ["src_subject_id", "tool", "region", "side", "unit", "measure", "label", "value"]


def extract(nidm_files: list[str], cde_files: list[str], region: str | None = None, unit: str | None = None,
            side: str | None = None) -> list[dict]:
    nidm = Graph()
    for f in nidm_files:
        nidm.parse(f)
    cde = Graph()
    for f in cde_files:
        cde.parse(f)
    measures = {}
    for m in set(cde.subjects(NIDM.isAbout, None)):
        reg = cde.value(m, NIDM.isAbout)
        u = cde.value(m, NIDM.hasUnit)
        lat = cde.value(m, NIDM.hasLaterality)
        if region and str(reg) != region:
            continue
        if unit and str(u) != unit:
            continue
        if side and str(lat or "").lower() != side.lower():
            continue
        measures[m] = {"region": str(reg), "unit": str(u or ""), "side": str(lat or ""),
                       "label": str(cde.value(m, RDFS.label) or "")}
    tool_of = {a: TOOLS.get(str(t), str(t)) for a, t in nidm.subject_objects(NIDM.NIDM_0000164)}
    rows = []
    for act in set(nidm.subjects(PROV.qualifiedAssociation, None)):
        agents = [nidm.value(q, PROV.agent) for q in nidm.objects(act, PROV.qualifiedAssociation)]
        tools = [tool_of[a] for a in agents if a in tool_of]
        ids = [nidm.value(a, NDAR_ID) for a in agents if nidm.value(a, NDAR_ID) is not None]
        if not tools or not ids:
            continue
        for ent in nidm.subjects(PROV.wasGeneratedBy, act):
            for m, meta in measures.items():
                for v in nidm.objects(ent, m):
                    rows.append({"src_subject_id": str(ids[0]), "tool": tools[0], "measure": str(m),
                                 "value": float(v), **meta})
    rows.sort(key=lambda r: (r["src_subject_id"], r["tool"], r["side"], r["measure"]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nidm", action="append", required=True, help="site NIDM Turtle file (repeatable)")
    ap.add_argument("--cde", action="append", required=True, help="tool CDE Turtle file (repeatable)")
    ap.add_argument("--region", help="anatomy IRI, e.g. http://purl.obolibrary.org/obo/UBERON_0001954")
    ap.add_argument("--unit", default="mm^3", help='measure unit (default "mm^3"; "" for all)')
    ap.add_argument("--side", help="Left or Right")
    ap.add_argument("--out", help="CSV path (default: stdout)")
    a = ap.parse_args()
    rows = extract(a.nidm, a.cde, a.region, a.unit or None, a.side)
    fh = open(a.out, "w", newline="") if a.out else sys.stdout
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    if a.out:
        fh.close()
        print(f"{a.out}: {len(rows)} values, {len({r['src_subject_id'] for r in rows})} participants", file=sys.stderr)


if __name__ == "__main__":
    main()
