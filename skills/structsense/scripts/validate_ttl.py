"""Gate a structsense Turtle file before handoff: OWL vocabulary + SHACL + shape of the graph.

Four layers, all on by default:

  1. OWL vocabulary (against default_ontology/named_entity_ontology.owl):
       - every ner: class used in rdf:type is declared
       - every ner: predicate is a declared Object/Datatype/Annotation property
       - rdfs:domain / rdfs:range under the full subclass closure
       - no untyped per-paper node used as the object of a ner: object property
       - one OntologyConcept node per conceptIdentifier
  2. SHACL (default_ontology/named_entity_shapes.ttl, via pyshacl with the
     ontology as ont_graph and RDFS inference): cardinalities, datatypes, key
     guardrails, "no external IRI without a tool-backed concept", causal
     versioning and the interventional-evidence rule for non-hypothetical claims.
  2b. Policy (default_ontology/ttl_config.json): generic_keys guardrail,
     interventional_evidence_bases for non-hypothetical claims, and prefix
     consistency — every conceptIdentifier uses the registry's canonical prefix
     and expands to its conceptIRI (scripts/prefixes.py).
  3. Graph shape: rdfs:label on every per-paper node, and ONE connected
     component per file (literals and rdf:type edges ignored) — a disconnected
     island is an entity or claim that lost its provenance.
  4. Optional --check-ols: every conceptIRI resolves in EBI OLS4 (catches
     fabricated ids; cached in .ols_cache.json). Existence only — whether the
     concept MEANS the entity is the mapping judge's job.

Exit code 0 = valid (warnings allowed), 1 = violations, 2 = usage/parse error.

Usage:
    python -m scripts.validate_ttl paper.ttl
    python -m scripts.validate_ttl paper.ttl --json report.json --check-ols
    python -m scripts.validate_ttl --ontology other.owl --shapes other_shapes.ttl --ns https://x.org/ner/ paper.ttl

Each file is validated on its own. Entity and concept nodes are shared UUIDs, so a
merged multi-paper file validates too.

A resource KG (any node typed in https://brainkb.org/resource/) is recognised and
gated against the BrainKB Resource Ontology instead: brainkb_resource_ontology.owl +
brainkb_resource_shapes.ttl (scripts/resource_kg.py validate). A licence the paper
never states is a source-silence finding, reported as a warning, not a violation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import rdflib
    from rdflib import OWL, RDF, RDFS, BNode, Literal, URIRef
except ImportError:
    sys.stderr.write("rdflib is required: pip install 'rdflib>=7,<8'\n")
    sys.exit(2)

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ONTOLOGY = SKILL_DIR / "default_ontology" / "named_entity_ontology.owl"
DEFAULT_SHAPES = SKILL_DIR / "default_ontology" / "named_entity_shapes.ttl"
DEFAULT_NS = "https://brainkb.org/ner/"
DEFAULT_KB_NS = "https://brainkb.org/kb/"
DEFAULT_TTL_CONFIG = SKILL_DIR / "default_ontology" / "ttl_config.json"


def load_ontology(path: Path) -> dict:
    ont = rdflib.Graph()
    ont.parse(str(path))
    classes = set(ont.subjects(RDF.type, OWL.Class))
    objprops = set(ont.subjects(RDF.type, OWL.ObjectProperty))
    dtprops = set(ont.subjects(RDF.type, OWL.DatatypeProperty))
    annprops = set(ont.subjects(RDF.type, OWL.AnnotationProperty))
    individuals = {s for s in ont.subjects(RDF.type, None)
                   if isinstance(s, URIRef) and s not in classes
                   and s not in objprops | dtprops | annprops}
    parents = defaultdict(set)
    for c, sup in ont.subject_objects(RDFS.subClassOf):
        if isinstance(sup, URIRef):
            parents[c].add(sup)
    closure: dict = {}

    def ancestors(c):
        if c not in closure:
            seen, stack = set(), list(parents.get(c, ()))
            while stack:
                p = stack.pop()
                if p not in seen:
                    seen.add(p)
                    stack.extend(parents.get(p, ()))
            closure[c] = seen
        return closure[c]

    props = objprops | dtprops | annprops
    return {
        "graph": ont, "classes": classes, "props": props, "objprops": objprops,
        "individuals": individuals, "ancestors": ancestors,
        "dom": {p: {d for d in ont.objects(p, RDFS.domain) if isinstance(d, URIRef)} for p in props},
        "rng": {p: {r for r in ont.objects(p, RDFS.range) if isinstance(r, URIRef)} for p in props},
    }


def check_vocabulary(ont: dict, data: rdflib.Graph, ns: str, kb_ns: str) -> dict[str, set[str]]:
    issues: dict[str, set[str]] = defaultdict(set)
    types = defaultdict(set)
    for s, t in data.subject_objects(RDF.type):
        types[s].add(t)
    # Controlled-vocabulary individuals (ner:causal-basis/..., ner:mapping-status/...)
    # are typed in the ontology, not in the data — look them up there.
    for ind in ont["individuals"]:
        for t in ont["graph"].objects(ind, RDF.type):
            types[ind].add(t)

    def instance_of(node, cls):
        return any(t == cls or cls in ont["ancestors"](t) for t in types.get(node, ()))

    for t in set(data.objects(None, RDF.type)):
        if str(t).startswith(ns) and t not in ont["classes"]:
            issues["undeclared class"].add(str(t))
    for p in set(data.predicates()):
        if str(p).startswith(ns) and p not in ont["props"]:
            issues["undeclared property"].add(str(p))
    for o in set(data.objects()):
        if isinstance(o, URIRef) and str(o).startswith(ns) and "/" in str(o)[len(ns):] \
                and o not in ont["individuals"]:
            issues["undeclared controlled term"].add(str(o))

    for s, p, o in data:
        if not str(p).startswith(ns):
            continue
        for d in ont["dom"].get(p, ()):
            if str(d).startswith(ns) and not instance_of(s, d):
                issues["domain violation"].add(f"{p.split('/')[-1]} on {s} (expects {d.split('/')[-1]})")
        if p in ont["objprops"]:
            for r in ont["rng"].get(p, ()):
                if str(r).startswith(ns) and not instance_of(o, r):
                    issues["range violation"].add(f"{p.split('/')[-1]} -> {o} (expects {r.split('/')[-1]})")

    for s, p, o in data:
        if p in ont["objprops"] and isinstance(o, URIRef) and str(o).startswith(kb_ns) and o not in types:
            issues["untyped per-paper node referenced"].add(str(o))

    per_id = defaultdict(set)
    for c, ident in data.subject_objects(URIRef(ns + "conceptIdentifier")):
        per_id[str(ident)].add(c)
    for ident, nodes in per_id.items():
        if len(nodes) > 1:
            issues["duplicate OntologyConcept for identifier"].add(f"{ident} ({len(nodes)} nodes)")
    return issues


def check_graph_shape(data: rdflib.Graph, kb_ns: str) -> tuple[dict[str, set[str]], int]:
    issues: dict[str, set[str]] = defaultdict(set)
    local = {s for s in data.subjects() if isinstance(s, URIRef) and str(s).startswith(kb_ns)}
    for s in local:
        if (s, RDFS.label, None) not in data:
            issues["per-paper node without rdfs:label"].add(str(s))

    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s, p, o in data:
        if isinstance(o, Literal) or p == RDF.type:
            continue
        # Only per-paper nodes form the component; an external IRI (obo:CL_...) that
        # two entities share would otherwise glue islands together and hide a break.
        if str(s).startswith(kb_ns):
            find(s)
            if str(o).startswith(kb_ns):
                parent[find(s)] = find(o)
    roots = defaultdict(list)
    for n in local | {x for x in parent if str(x).startswith(kb_ns)}:
        roots[find(n)].append(n)
    comps = sorted(roots.values(), key=len, reverse=True)
    if len(comps) > 1:
        for comp in comps[1:]:
            sample = ", ".join(sorted(str(n).rsplit("/", 1)[-1] for n in comp)[:4])
            issues["disconnected component"].add(f"{len(comp)} node(s): {sample}")
    return issues, len(comps)


def check_policy(data: rdflib.Graph, ns: str, ttl_config: Path, ont: Optional[dict] = None
                 ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Policy that is configuration, not structure (default_ontology/ttl_config.json),
    plus prefix consistency through the one prefix registry (scripts/prefixes.py)."""
    issues: dict[str, set[str]] = defaultdict(set)
    warns: dict[str, set[str]] = defaultdict(set)
    cfg = json.loads(Path(ttl_config).read_text()) if Path(ttl_config).is_file() else {}
    N = rdflib.Namespace(ns)
    # relations: only the configured predicates; targets of the expected kind (warning)
    rel = {k: URIRef(v) for k, v in (cfg.get("relation_predicates") or {}).items() if not k.startswith("_")}
    by_iri = {v: k for k, v in rel.items()}
    hints = {k: v for k, v in (cfg.get("relation_range_hints") or {}).items() if not k.startswith("_")}
    for s_, p_, o_ in data:
        ps = str(p_)
        if not (ps.startswith("http://purl.obolibrary.org/obo/RO_") or ps.startswith("http://purl.obolibrary.org/obo/BFO_")):
            continue
        name = by_iri.get(p_)
        if name is None:
            issues["relation predicate not in ttl_config.json relation_predicates"].add(ps)
            continue
        if ont and name in hints:
            want = {URIRef(ns + c) for c in hints[name]}
            types = set(data.objects(o_, RDF.type))
            if types and not any(t in want or (want & ont["ancestors"](t)) for t in types):
                warns[f"{name} target outside relation_range_hints ({', '.join(hints[name])})"].add(
                    f"{s_} -> {o_}")
    # identity: every node this pipeline mints is <iri.base><uuid>, cell output included.
    # Only scheme "slug" (debugging) lifts the rule, and it is reported as a warning.
    iri_cfg = cfg.get("iri") or {}
    base = iri_cfg.get("base", "https://brainkb.org/kb/")
    uuid_iri = re.compile(re.escape(base) + r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    bucket = warns if iri_cfg.get("scheme") == "slug" else issues
    # no blank nodes, ever: an unnamed node cannot be merged, referenced or reviewed
    # across papers. Not lifted by scheme "slug" either.
    for s_, p_, o_ in data:
        for x in (s_, o_):
            if isinstance(x, BNode):
                types = ", ".join(sorted(str(t) for t in data.objects(x, RDF.type))) or "untyped"
                issues["blank node; every node must be a <base><uuid> IRI (ttl_config.json iri)"].add(
                    f"_:{x} ({types}) via {p_}")
    for s_, t_ in data.subject_objects(RDF.type):
        if isinstance(s_, BNode):
            continue
        ss = str(s_)
        if ss.startswith(ns):  # ontology vocabulary / controlled individuals
            continue
        if (ss.startswith(base) or str(t_).startswith(ns)) and not uuid_iri.match(ss):
            bucket[f"instance IRI is not <{base}><uuid> (ttl_config.json iri)"].add(ss)
    for o_ in set(data.objects()):
        if isinstance(o_, URIRef) and str(o_).startswith(base) and not uuid_iri.match(str(o_)):
            bucket[f"instance IRI is not <{base}><uuid> (ttl_config.json iri)"].add(str(o_))
    # labels are names, not explanations: prose belongs in a string property
    cap = int((cfg.get("labels") or {}).get("max_length") or 0)
    if cap:
        for s_, l_ in data.subject_objects(RDFS.label):
            if len(str(l_)) > cap:
                issues[f"rdfs:label longer than {cap} chars (ttl_config.json labels): put prose in "
                       f"rdfs:comment / a string property"].add(f"{s_}: {str(l_)[:80]}")
    generic = set(cfg.get("generic_keys") or [])
    for e, k in data.subject_objects(N.normalizedEntityKey):
        if str(k) in generic:
            issues["normalizedEntityKey on the generic_keys guardrail list (ttl_config.json)"].add(f"{e} ({k})")
    basis_ns = ns + "causal-basis/"
    interventional = {basis_ns + b for b in cfg.get("interventional_evidence_bases") or []}
    for v in data.subjects(N.causalHypothetical, Literal(False)):
        bases = {str(b) for b in data.objects(v, N.causalEvidenceBasis)}
        if not bases & interventional:
            issues["causalHypothetical false without an interventional evidence basis"].add(str(v))
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from prefixes import PrefixRegistry
        reg = PrefixRegistry(ttl_config)
    except Exception as e:  # the registry is a check, not a dependency of the gate
        issues["prefix registry unavailable"].add(str(e))
        return issues, warns
    for c in data.subjects(RDF.type, N.OntologyConcept):
        iri, cid = data.value(c, N.conceptIRI), data.value(c, N.conceptIdentifier)
        if iri is None or cid is None:
            continue
        prefix = str(cid).split(":", 1)[0]
        canon = reg.canonical(prefix, iri=str(iri))
        if canon is None:
            issues["conceptIdentifier prefix not in the prefix registry"].add(f"{cid}")
        elif canon != prefix:
            issues["conceptIdentifier prefix not in canonical form"].add(f"{cid} (registry: {canon})")
        elif reg.expand(str(cid)) != str(iri):
            issues["conceptIdentifier does not expand to conceptIRI"].add(f"{cid} -> {reg.expand(str(cid))} != {iri}")
    for o in data.subjects(RDF.type, N.ExternalOntology):
        acr = data.value(o, N.ontologyAcronym)
        if acr is not None and reg.canonical(str(acr)) not in (str(acr), None):
            issues["ontologyAcronym not in canonical form"].add(f"{acr} (registry: {reg.canonical(str(acr))})")
    return issues, warns


def run_shacl(data: rdflib.Graph, ont: dict, shapes_path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], bool]:
    """Return (violations, warnings, ran)."""
    try:
        from pyshacl import validate as shacl_validate
    except ImportError:
        return {}, {"SHACL skipped": {"pyshacl not installed — pip install 'pyshacl>=0.26'"}}, False
    shapes = rdflib.Graph()
    shapes.parse(str(shapes_path), format="turtle")
    _conforms, report, _text = shacl_validate(
        data, shacl_graph=shapes, ont_graph=ont["graph"], inference="rdfs",
        abort_on_first=False, allow_warnings=True, advanced=True)
    if not isinstance(report, rdflib.Graph):
        # pyshacl returns a ValidationFailure when the SHAPES are unusable — a
        # broken gate must never read as a passing one.
        raise RuntimeError(f"SHACL shapes could not be applied: {getattr(report, 'message', report)}")
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    violations: dict[str, set[str]] = defaultdict(set)
    warnings: dict[str, set[str]] = defaultdict(set)
    for r in report.subjects(RDF.type, SH.ValidationResult):
        sev = report.value(r, SH.resultSeverity)
        focus = report.value(r, SH.focusNode)
        path = report.value(r, SH.resultPath)
        msg = report.value(r, SH.resultMessage)
        shape = report.value(r, SH.sourceShape)
        shape_name = str(shape).rsplit("/", 1)[-1] if isinstance(shape, URIRef) else ""
        where = str(path).rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(path, URIRef) else ""
        key = f"SHACL {shape_name or 'shape'}" + (f" [{where}]" if where else "") + f": {msg}"
        bucket = violations if sev == SH.Violation else warnings
        bucket[key].add(str(focus))
    return violations, warnings, True


def check_ols(concept_iris: set[str], cache_path: Path = Path(".ols_cache.json"), timeout: int = 15) -> set[str]:
    import urllib.parse
    import urllib.request
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}
    bad = set()
    for iri in sorted(concept_iris):
        if iri not in cache:
            url = f"https://www.ebi.ac.uk/ols4/api/terms?iri={urllib.parse.quote(iri, safe='')}"
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    cache[iri] = json.loads(r.read()).get("page", {}).get("totalElements", 0) > 0
            except Exception:
                cache[iri] = False  # network failure reads as unresolved; rerun to retry
        if not cache[iri]:
            bad.add(iri)
    cache_path.write_text(json.dumps(cache, indent=1))
    return bad


def validate_file(ttl: Path, *, ontology: Path = DEFAULT_ONTOLOGY, shapes: Path = DEFAULT_SHAPES,
                  ns: str = DEFAULT_NS, kb_ns: str = DEFAULT_KB_NS, use_shacl: bool = True,
                  ols: bool = False, ont: dict | None = None) -> dict:
    """Library entry point. Returns a report dict; `ok` is the gate."""
    data = rdflib.Graph()
    data.parse(str(ttl), format="turtle")
    if _is_resource_graph(data):
        return _resource_report(ttl, data)
    ont = ont or load_ontology(ontology)
    violations = check_vocabulary(ont, data, ns, kb_ns)
    pol_v, pol_w = check_policy(data, ns, DEFAULT_TTL_CONFIG, ont)
    violations.update(pol_v)
    shape_issues, components = check_graph_shape(data, kb_ns)
    violations.update(shape_issues)
    warnings: dict[str, set[str]] = {}
    shacl_ran = False
    if use_shacl:
        sv, sw, shacl_ran = run_shacl(data, ont, shapes)
        violations.update(sv)
        warnings = sw
    warnings = {**warnings, **pol_w}
    if ols:
        iris = {str(o) for o in data.objects(None, URIRef(ns + "conceptIRI"))}
        for iri in check_ols(iris):
            violations.setdefault("conceptIRI not found in OLS4", set()).add(iri)
    n_viol = sum(len(v) for v in violations.values())
    return {
        "file": str(ttl), "triples": len(data), "components": components,
        "ontology_classes": len(ont["classes"]), "ontology_properties": len(ont["props"]),
        "shacl": shacl_ran, "ok": n_viol == 0, "violation_count": n_viol,
        "warning_count": sum(len(v) for v in warnings.values()),
        "violations": {k: sorted(v) for k, v in sorted(violations.items())},
        "warnings": {k: sorted(v) for k, v in sorted(warnings.items())},
    }


def _is_resource_graph(data: rdflib.Graph) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from resource_kg import is_resource_graph
    return is_resource_graph(data)


def _resource_report(ttl: Path, data: rdflib.Graph) -> dict:
    """A resource KG (BrainKB Resource Ontology) is gated by resource_kg.validate_graph:
    the BKR shapes (default_ontology/brainkb_resource_shapes.ttl), the BKR + NER
    vocabularies and one connected component. Source-silence findings (no licence
    stated by the paper) are reported but never fail the gate."""
    from resource_kg import load_config, validate_graph
    cfg = load_config()
    r = validate_graph(data, cfg)
    violations: dict[str, set[str]] = {}
    for v in r["violations"]:
        violations.setdefault(f"[{v['shape']}] {v['message']}", set()).add(v["focus"])
    for p in r["problems"]:
        violations.setdefault(p, set()).add(str(ttl))
    warnings: dict[str, set[str]] = {}
    for f in r["source_silence_findings"]:
        warnings.setdefault(f"source silence (finding, not a defect) [{f['shape']}] {f['message']}", set()).add(f["focus"])
    for w in r["warnings"]:
        warnings.setdefault(f"[SHACL warning] {w['message']}", set()).add(f"{w['count']} node(s)")
    onto = rdflib.Graph().parse(cfg["ontology"], format="xml")
    return {
        "file": str(ttl), "kind": "resource_kg", "triples": len(data), "components": r["components"],
        "ontology_classes": len(set(onto.subjects(RDF.type, OWL.Class))),
        "ontology_properties": len(set(onto.subjects(RDF.type, OWL.ObjectProperty))
                                   | set(onto.subjects(RDF.type, OWL.DatatypeProperty))),
        "shacl": True, "ok": r["ok"], "violation_count": sum(len(v) for v in violations.values()),
        "warning_count": sum(len(v) for v in warnings.values()),
        "source_silence_findings": len(r["source_silence_findings"]),
        "violations": {k: sorted(v) for k, v in sorted(violations.items())},
        "warnings": {k: sorted(v) for k, v in sorted(warnings.items())},
    }


def _print(report: dict, max_report: int) -> None:
    print(f"validated {report['triples']} triples in {report['file']} against "
          f"{report['ontology_classes']} classes / {report['ontology_properties']} properties"
          f"{' + SHACL shapes' if report['shacl'] else ''}; {report['components']} component(s)")
    for title, block in (("VIOLATION", report["violations"]), ("warning", report["warnings"])):
        for k, v in block.items():
            print(f"\n[{title}] {k} — {len(v)}:")
            for x in v[:max_report]:
                print(f"    {x}")
            if len(v) > max_report:
                print(f"    ... and {len(v) - max_report} more")
    if report["ok"]:
        print(f"\nRESULT: VALID — 0 violations, {report['warning_count']} warning(s)")
    else:
        print(f"\nRESULT: {report['violation_count']} violation(s), {report['warning_count']} warning(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ttl", nargs="+", type=Path, help="Turtle file(s); each is validated on its own")
    ap.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY)
    ap.add_argument("--shapes", type=Path, default=DEFAULT_SHAPES)
    ap.add_argument("--ns", default=DEFAULT_NS, help="target ontology namespace")
    ap.add_argument("--kb-ns", default=DEFAULT_KB_NS, help="per-paper IRI namespace")
    ap.add_argument("--no-shacl", action="store_true", help="skip the SHACL layer")
    ap.add_argument("--check-ols", action="store_true", help="also resolve every conceptIRI in OLS4")
    ap.add_argument("--json", type=Path, help="write the report(s) as JSON")
    ap.add_argument("--max-report", type=int, default=10)
    args = ap.parse_args()
    ns = args.ns.rstrip("/") + "/"
    try:
        ont = load_ontology(args.ontology)
    except Exception as e:
        sys.stderr.write(f"failed to load ontology: {e}\n")
        return 2
    reports = []
    for f in args.ttl:
        try:
            rep = validate_file(f, shapes=args.shapes, ns=ns, kb_ns=args.kb_ns,
                                use_shacl=not args.no_shacl, ols=args.check_ols, ont=ont)
        except Exception as e:
            sys.stderr.write(f"PARSE ERROR in {f}: {e}\n")
            return 2
        _print(rep, args.max_report)
        reports.append(rep)
    if args.json:
        args.json.write_text(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2) + "\n")
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
