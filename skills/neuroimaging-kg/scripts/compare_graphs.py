# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Compare knowledge graphs built by different contributors before harmonising them.

Each contributor is one or more Turtle files (e.g. a site NIDM file plus the CDE files that define its
measures) and stays a separate graph: nothing is merged or rewritten here. For every pair of
contributors this reports:

1. Identifier overlap — participants (ndar:src_subject_id by default, or --id-property), datasets
   (dcat:Dataset IRI, with version differences), publications (DOI, case-insensitive).
2. Vocabulary alignment — data elements that share a concept (nidm:isAbout: InterLex, UBERON, …), and
   elements whose concept the other contributor lacks. The latter become *mapping candidates* for a
   person to review (proposed skos:exactMatch when source variables are identical, else closeMatch).
   Candidates go to a review file; no mapping is asserted, and never owl:sameAs.
3. Conflicts — the same participant with the same concept (and laterality) holding values that differ
   beyond --rtol, flagged when units or labels also differ (same term, different measure); and named
   cohorts (same rdfs:label or IRI) whose memberships differ.

Values are read with the NIDM pattern: an entity prov:wasGeneratedBy an activity whose qualified
association names the participant; the entity's predicate is the data element.

Usage:
  uv run scripts/compare_graphs.py --graph ours=graph/phenotype.ttl,graph/cohort.ttl \
      --graph repronim=data/Caltech_nidm.ttl,data/freesurfer_cde.ttl --attributed ours="Our lab" \
      --attributed repronim="ReproNim simple2_NIDM_examples@e0dff17" --normalize int \
      --out compare.json --markdown compare.md --review mapping_review.ttl --attribution contributors.ttl
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import itertools
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from rdflib import RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

PROV = Namespace("http://www.w3.org/ns/prov#")
NIDM = Namespace("http://purl.org/nidash/nidm#")
NKG = Namespace("urn:neuroimaging-kg:")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
OWL = Namespace("http://www.w3.org/2002/07/owl#")
SCHEMA = Namespace("http://schema.org/")
SRC_ID = "https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id"
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)
MISSING = {"nan", "-9999", "-9999.0", ""}


def norm_id(v: str, how: str) -> str:
    v = str(v).strip()
    if how == "int":
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    return v


def norm_concept(iri: str) -> str:
    """InterLex appears as .../ilx_X and .../base/ilx_X; treat them as one concept."""
    return iri.replace("http://uri.interlex.org/base/", "http://uri.interlex.org/")


def load(files: list[str]) -> Graph:
    g = Graph()
    for f in files:
        g.parse(f)
    return g


def profile(g: Graph, id_property: str, how: str) -> dict:
    ids = {a: norm_id(v, how) for a, v in g.subject_objects(URIRef(id_property))}
    elements = {}
    for el, concept in g.subject_objects(NIDM.isAbout):
        elements.setdefault(el, set()).add(norm_concept(str(concept)))
    for el in set(g.subjects(RDF.type, NIDM.DataElement)) | set(g.subjects(RDF.type, NIDM.PersonalDataElement)) \
            | set(g.subjects(NIDM.sourceVariable, None)):
        elements.setdefault(el, set())
    meta = {el: {"label": str(g.value(el, RDFS.label) or ""), "source_variable": str(g.value(el, NIDM.sourceVariable) or ""),
                 "unit": str(g.value(el, NIDM.hasUnit) or ""), "side": str(g.value(el, NIDM.hasLaterality) or "").lower(),
                 "concepts": sorted(c)} for el, c in elements.items()}
    values: dict[tuple, list] = defaultdict(list)
    for act in set(g.subjects(PROV.qualifiedAssociation, None)):
        pids = [ids[ag] for q in g.objects(act, PROV.qualifiedAssociation) for ag in g.objects(q, PROV.agent) if ag in ids]
        if not pids:
            continue
        for ent in g.subjects(PROV.wasGeneratedBy, act):
            for p, v in g.predicate_objects(ent):
                if p in meta and str(v).strip().lower() not in MISSING:
                    for c in meta[p]["concepts"]:
                        values[(pids[0], c, meta[p]["side"])].append((p, str(v)))
    datasets = {str(d): sorted({str(v) for prop in (DCAT.version, OWL.versionInfo, SCHEMA.version)
                                for v in g.objects(d, prop)}) for d in g.subjects(RDF.type, DCAT.Dataset)}
    dois = set()
    for t in g:
        for term in t:
            if isinstance(term, (URIRef, Literal)) and ("doi" in str(term).lower()):
                m = DOI_RE.search(str(term))
                if m:
                    dois.add(m.group(1).lower().rstrip("."))
    cohorts = {}
    for c in set(g.subjects(RDF.type, NKG.Cohort)):
        key = str(g.value(c, RDFS.label) or c)
        cohorts[key] = {ids.get(m, str(m)) for m in g.objects(c, PROV.hadMember)}
    return {"ids": set(ids.values()), "meta": meta, "values": values, "datasets": datasets, "dois": dois, "cohorts": cohorts}


def _num(v: str):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def compare_pair(A: dict, B: dict, rtol: float, min_similarity: float) -> dict:
    out: dict = {}
    out["participants"] = {"matched": len(A["ids"] & B["ids"]), "only_a": sorted(A["ids"] - B["ids"], key=lambda s: (len(s), s)),
                           "only_b": sorted(B["ids"] - A["ids"], key=lambda s: (len(s), s))}
    ds = {"matched": sorted(set(A["datasets"]) & set(B["datasets"])), "only_a": sorted(set(A["datasets"]) - set(B["datasets"])),
          "only_b": sorted(set(B["datasets"]) - set(A["datasets"])), "version_mismatch": []}
    for d in ds["matched"]:
        if A["datasets"][d] and B["datasets"][d] and A["datasets"][d] != B["datasets"][d]:
            ds["version_mismatch"].append({"dataset": d, "a": A["datasets"][d], "b": B["datasets"][d]})
    out["datasets"] = ds
    out["publications"] = {"matched": sorted(A["dois"] & B["dois"]), "only_a": sorted(A["dois"] - B["dois"]),
                           "only_b": sorted(B["dois"] - A["dois"])}

    ca = {c for m in A["meta"].values() for c in m["concepts"]}
    cb = {c for m in B["meta"].values() for c in m["concepts"]}
    cands = []
    for (X, Y, xname, yname, other_concepts) in ((A, B, "a", "b", cb), (B, A, "b", "a", ca)):
        for el, m in X["meta"].items():
            if m["concepts"] and set(m["concepts"]) & other_concepts:
                continue
            best, score = None, 0.0
            for el2, m2 in Y["meta"].items():
                if m["concepts"] and m2["concepts"] and set(m["concepts"]) & set(m2["concepts"]):
                    continue
                for s1, s2 in ((m["source_variable"], m2["source_variable"]), (m["label"], m2["label"])):
                    if s1 and s2:
                        r = 1.0 if s1.lower() == s2.lower() else difflib.SequenceMatcher(None, s1.lower(), s2.lower()).ratio()
                        if r > score:
                            best, score = el2, r
            if best is not None and score >= min_similarity:
                m2 = Y["meta"][best]
                exact = bool(m["source_variable"]) and m["source_variable"].lower() == m2["source_variable"].lower()
                pair = {xname: str(el), yname: str(best)}
                cands.append({"a": pair["a"], "b": pair["b"], "proposed": "skos:exactMatch" if exact else "skos:closeMatch",
                              "similarity": round(score, 2), "basis": "same source variable" if exact else "label/variable similarity",
                              "concepts": {xname: m["concepts"], yname: m2["concepts"]}, "review_status": "proposed"})
    seen, uniq = set(), []
    for c in cands:
        if (c["a"], c["b"]) not in seen:
            seen.add((c["a"], c["b"]))
            uniq.append(c)
    out["vocabulary"] = {"shared_concepts": sorted(ca & cb), "only_a_concepts": sorted(ca - cb), "only_b_concepts": sorted(cb - ca),
                         "elements_without_concept": {"a": sum(not m["concepts"] for m in A["meta"].values()),
                                                      "b": sum(not m["concepts"] for m in B["meta"].values())},
                         "mapping_candidates": uniq}

    conflicts, ambiguous = [], []
    for key in sorted(set(A["values"]) & set(B["values"])):
        va, vb = A["values"][key], B["values"][key]
        pairs = _pair_elements(va, vb, A["meta"], B["meta"])
        if pairs is None:
            ambiguous.append({"participant": key[0], "concept": key[1], "side": key[2],
                              "elements_a": sorted({str(p) for p, _ in va}), "elements_b": sorted({str(p) for p, _ in vb})})
            continue
        for (pa, xa), (pb, xb) in pairs:
            rec = _value_conflict(key, pa, xa, pb, xb, A["meta"], B["meta"], rtol)
            if rec:
                conflicts.append(rec)
    return _finish(out, A, B, conflicts, ambiguous)


def _pair_elements(va, vb, meta_a, meta_b):
    """One element per side: compare directly. Several sharing a concept: pair by source variable, else ambiguous."""
    ea, eb = {p for p, _ in va}, {p for p, _ in vb}
    if len(ea) == 1 and len(eb) == 1:
        return [(va[0], vb[0])]
    by_a = {meta_a[p]["source_variable"].lower(): (p, x) for p, x in va if meta_a[p]["source_variable"]}
    by_b = {meta_b[p]["source_variable"].lower(): (p, x) for p, x in vb if meta_b[p]["source_variable"]}
    common = sorted(set(by_a) & set(by_b))
    if not common or len(by_a) < len(ea) or len(by_b) < len(eb):
        return None
    return [(by_a[k], by_b[k]) for k in common]


def _value_conflict(key, pa, xa, pb, xb, meta_a, meta_b, rtol):
    na, nb = _num(xa), _num(xb)
    if na is not None and nb is not None:
        differ = abs(na - nb) > rtol * max(abs(na), abs(nb), 1e-12)
    else:
        differ = xa.strip().lower() != xb.strip().lower()
    if not differ:
        return None
    ma, mb = meta_a[pa], meta_b[pb]
    rec = {"participant": key[0], "concept": key[1], "side": key[2], "a": xa, "b": xb,
           "element_a": str(pa), "element_b": str(pb), "label_a": ma["label"], "label_b": mb["label"],
           "unit_a": ma["unit"], "unit_b": mb["unit"],
           "definitions_differ": ma["unit"] != mb["unit"] or (bool(ma["label"]) and bool(mb["label"])
                                                              and difflib.SequenceMatcher(None, ma["label"].lower(), mb["label"].lower()).ratio() < 0.5)}
    if na is not None and nb is not None and min(abs(na), abs(nb)) > 0:
        rec["ratio"] = round(max(abs(na), abs(nb)) / min(abs(na), abs(nb)), 2)
    return rec




def _finish(out, A, B, conflicts, ambiguous):
    cohorts = []
    for name in sorted(set(A["cohorts"]) & set(B["cohorts"])):
        a_only, b_only = A["cohorts"][name] - B["cohorts"][name], B["cohorts"][name] - A["cohorts"][name]
        if a_only or b_only:
            cohorts.append({"cohort": name, "only_a": sorted(a_only), "only_b": sorted(b_only)})
    out["conflicts"] = {"values": conflicts, "ambiguous_concepts": ambiguous, "cohorts": cohorts,
                        "compared_values": len(set(A["values"]) & set(B["values"]))}
    return out


def compare(contributors: dict[str, list[str]], id_property: str = SRC_ID, normalize: str = "int", rtol: float = 0.01,
            min_similarity: float = 0.6) -> dict:
    profiles = {name: profile(load(files), id_property, normalize) for name, files in contributors.items()}
    rep = {"contributors": {n: {"files": [{"path": f, "sha256": hashlib.sha256(Path(f).read_bytes()).hexdigest()} for f in fs],
                                "participants": len(profiles[n]["ids"]), "data_elements": len(profiles[n]["meta"])}
                            for n, fs in contributors.items()},
           "settings": {"id_property": id_property, "normalize": normalize, "rtol": rtol, "min_similarity": min_similarity},
           "pairs": {}}
    for a, b in itertools.combinations(contributors, 2):
        rep["pairs"][f"{a}|{b}"] = compare_pair(profiles[a], profiles[b], rtol, min_similarity)
    return rep


def write_review(rep: dict, path: str | Path) -> None:
    """Mapping candidates for a person to accept or reject. Proposals only: no skos/owl triple is asserted."""
    g = Graph()
    g.bind("nkg", NKG)
    for pair, res in rep["pairs"].items():
        for i, c in enumerate(res["vocabulary"]["mapping_candidates"]):
            n = URIRef(f"urn:neuroimaging-kg:mapping-candidate/{pair.replace('|', '--')}/{i}")
            g.add((n, RDF.type, NKG.MappingCandidate))
            g.add((n, NKG.subjectElement, URIRef(c["a"])))
            g.add((n, NKG.objectElement, URIRef(c["b"])))
            g.add((n, NKG.proposedRelation, Literal(c["proposed"])))
            g.add((n, NKG.similarity, Literal(c["similarity"])))
            g.add((n, NKG.basis, Literal(c["basis"])))
            g.add((n, NKG.reviewStatus, Literal("proposed")))
            g.add((n, NKG.betweenGraphs, Literal(pair)))
    g.serialize(path, format="turtle")


def write_attribution(rep: dict, path: str | Path, attributed: dict[str, str]) -> None:
    """Each contributor's graph as a PROV entity attributed to its contributor, with its source files' hashes."""
    g = Graph()
    g.bind("prov", PROV)
    for name, info in rep["contributors"].items():
        gn = URIRef(f"urn:neuroimaging-kg:contributor-graph/{name}")
        who = URIRef(f"urn:neuroimaging-kg:contributor/{re.sub(r'[^A-Za-z0-9._-]+', '-', attributed.get(name, name))}")
        g.add((gn, RDF.type, PROV.Entity))
        g.add((gn, RDFS.label, Literal(f"graph contributed by {attributed.get(name, name)}")))
        g.add((gn, PROV.wasAttributedTo, who))
        g.add((who, RDF.type, PROV.Agent))
        g.add((who, RDFS.label, Literal(attributed.get(name, name))))
        for f in info["files"]:
            src = URIRef(f"urn:sha256:{f['sha256']}")
            g.add((src, RDF.type, PROV.Entity))
            g.add((src, RDFS.label, Literal(Path(f["path"]).name)))
            g.add((gn, PROV.hadPrimarySource, src))
    g.serialize(path, format="turtle")


def to_markdown(rep: dict) -> str:
    lines = ["# Comparison of contributors' graphs", ""]
    for n, i in rep["contributors"].items():
        lines.append(f"- **{n}**: {i['participants']} participants, {i['data_elements']} data elements, "
                     f"{len(i['files'])} file(s)")
    for pair, r in rep["pairs"].items():
        a, b = pair.split("|")
        p, v, c = r["participants"], r["vocabulary"], r["conflicts"]
        lines += ["", f"## {a} vs {b}", "", "| Identifiers | Matched | Only in " + a + " | Only in " + b + " |", "|---|---|---|---|",
                  f"| participants | {p['matched']} | {len(p['only_a'])} | {len(p['only_b'])} |",
                  f"| datasets | {len(r['datasets']['matched'])} | {len(r['datasets']['only_a'])} | {len(r['datasets']['only_b'])} |",
                  f"| publications (DOI) | {len(r['publications']['matched'])} | {len(r['publications']['only_a'])} | {len(r['publications']['only_b'])} |", ""]
        if p["only_a"] or p["only_b"]:
            lines.append(f"Only in {a}: {', '.join(p['only_a'][:20]) or '—'}. Only in {b}: {', '.join(p['only_b'][:20]) or '—'}.")
        for vm in r["datasets"]["version_mismatch"]:
            lines.append(f"Dataset {vm['dataset']}: version {vm['a']} vs {vm['b']}.")
        lines += ["", f"**Vocabulary.** {len(v['shared_concepts'])} shared concepts; {len(v['only_a_concepts'])} only in {a}, "
                  f"{len(v['only_b_concepts'])} only in {b}; elements without any concept: {v['elements_without_concept']['a']} ({a}), "
                  f"{v['elements_without_concept']['b']} ({b}). {len(v['mapping_candidates'])} mapping candidates proposed for review "
                  "(none asserted)."]
        for m in v["mapping_candidates"][:10]:
            lines.append(f"- {m['proposed']} ? `{m['a']}` ↔ `{m['b']}` ({m['basis']}, {m['similarity']})")
        lines += ["", f"**Conflicts.** {c['compared_values']} participant×concept values compared; {len(c['values'])} differ beyond "
                  f"rtol={rep['settings']['rtol']}; {len(c['ambiguous_concepts'])} not compared because one side has several elements "
                  f"for the same concept; {len(c['cohorts'])} cohorts with different members."]
        for x in c["values"][:10]:
            flag = " — **definitions differ**" if x["definitions_differ"] else ""
            ratio = f" (×{x['ratio']})" if "ratio" in x else ""
            lines.append(f"- participant {x['participant']}, {x['concept'].rsplit('/', 1)[-1]} {x['side']}: "
                         f"{x['a']} ({x['label_a'] or '—'}, {x['unit_a'] or 'no unit'}) vs {x['b']} ({x['label_b'] or '—'}, "
                         f"{x['unit_b'] or 'no unit'}){ratio}{flag}")
        for x in c["cohorts"]:
            lines.append(f"- cohort “{x['cohort']}”: only in {a} {x['only_a']}, only in {b} {x['only_b']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", action="append", required=True, help="NAME=file1.ttl[,file2.ttl...] (repeat per contributor)")
    ap.add_argument("--attributed", action="append", default=[], help='NAME="who contributed it" (for the attribution graph)')
    ap.add_argument("--id-property", default=SRC_ID)
    ap.add_argument("--normalize", choices=["int", "strip"], default="int")
    ap.add_argument("--rtol", type=float, default=0.01, help="relative tolerance for numeric values (default 1%)")
    ap.add_argument("--min-similarity", type=float, default=0.6)
    ap.add_argument("--out", required=True, help="JSON report")
    ap.add_argument("--markdown", help="Markdown summary")
    ap.add_argument("--review", help="Turtle file of mapping candidates to review")
    ap.add_argument("--attribution", help="Turtle file attributing each contributor's graph")
    a = ap.parse_args()
    contributors = {k: v.split(",") for k, v in (g.split("=", 1) for g in a.graph)}
    rep = compare(contributors, a.id_property, a.normalize, a.rtol, a.min_similarity)
    Path(a.out).write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    if a.markdown:
        Path(a.markdown).write_text(md)
    if a.review:
        write_review(rep, a.review)
    if a.attribution:
        write_attribution(rep, a.attribution, dict(x.split("=", 1) for x in a.attributed))
    print(md)


if __name__ == "__main__":
    main()
