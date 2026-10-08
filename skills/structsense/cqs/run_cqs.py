"""Execute standalone StructSense competency queries on canonical Turtle files/directories.

Example: python cqs/run_cqs.py /path/to/output --report cq-results.json
Requires rdflib; optionally install pyoxigraph for large corpora. Empty answers do not
fail execution. Reports distinguish missing prerequisite records from empty answers.

Resource KGs (BrainKB Resource Ontology):
    python cqs/run_cqs.py out/ --cqs cqs/brainkb_resource_ontology_CQs.md \
        --with-ontology default_ontology/brainkb_resource_ontology.owl --entail
--entail queries the OWL-RL closure of data + ontology (needs owlrl): bkr:hasScope and
bkr:appliedToConcept are entailed, never asserted.
"""
from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path
import rdflib

HERE = Path(__file__).resolve().parent
N = 'https://brainkb.org/ner/'
REQUIRES = {'CQ9': 'ConceptMappingDecision', 'CQ23': 'EntityClassification',
            'CQ24b': 'ReviewDecision', 'CQ42': 'ReviewDecision', 'CQ43': 'ChangeRecord',
            'CQ44': 'MappingRemovedChange', 'CQ45': 'ConceptMappingDecision',
            'CQ46': 'ValidationReport', 'CQ13': 'CausalChain',
            'CQ25': 'RelationAssertion', 'CQ51': 'RelationAssertion', 'CQ55': 'RelationAssertion'}


def load_cqs(md: Path) -> list[tuple[str, str, str]]:
    return [(m[1], ' '.join(m[2].split()), m[3]) for m in re.finditer(
        r'\*\*(CQ\d+[a-z]?)(?:/\d+)? — (.*?)\*\*[^`]*?```sparql\n(.*?)```', md.read_text(), re.S)]


def discover(paths):
    files = set()
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise ValueError(f'Input does not exist: {p}')
        candidates = p.rglob('*.ttl') if p.is_dir() else [p]
        for f in candidates:
            if f.name.endswith(('.entities.ttl', '.invalid.ttl')):
                continue
            if p.is_dir() and any(x.startswith('.') for x in f.relative_to(p).parts):
                continue
            files.add(f.resolve())
    if not files:
        raise ValueError('No canonical Turtle files found')
    return sorted(files)


def parameterize(query, params):
    for name, value in params.items():
        if value is None:
            continue
        if name == 'requestedTerm':
            if not re.fullmatch(r'https?://[^\s<>"{}|\\^`]+', value):
                raise ValueError('--term-iri must be an absolute HTTP(S) IRI')
            term = rdflib.URIRef(value).n3()
        else:
            term = rdflib.Literal(value).n3()
        query = query.replace(f'VALUES ?{name} {{ UNDEF }}', f'VALUES ?{name} {{ {term} }}')
    return query


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('ttl', nargs='+', type=Path)
    ap.add_argument('--cqs', type=Path, default=HERE / 'named_entity_ontology_CQs.md')
    ap.add_argument('--with-ontology', type=Path)
    ap.add_argument('--entail', action='store_true',
                    help='query the OWL-RL closure of the data + --with-ontology (needs owlrl)')
    ap.add_argument('--only', help='Comma-separated IDs, e.g. CQ24,CQ25')
    ap.add_argument('--rows', type=int, default=3)
    ap.add_argument('--engine', choices=['auto', 'rdflib', 'oxigraph'], default='auto')
    ap.add_argument('--report', type=Path)
    ap.add_argument('--entity-key'); ap.add_argument('--doi')
    ap.add_argument('--ontology-acronym'); ap.add_argument('--term-iri')
    args = ap.parse_args()
    try:
        files = discover(args.ttl)
        cqs = load_cqs(args.cqs)
        if not cqs:
            raise ValueError('No competency queries found')
        wanted = set(args.only.split(',')) if args.only else {x[0] for x in cqs}
        unknown = wanted - {x[0] for x in cqs}
        if unknown:
            raise ValueError(f'Unknown query IDs: {sorted(unknown)}')
        params = dict(requestedKey=args.entity_key, requestedDoi=args.doi,
                      requestedOntology=args.ontology_acronym, requestedTerm=args.term_iri)
        queries = [(cq, title, parameterize(q, params)) for cq, title, q in cqs if cq in wanted]
    except ValueError as e:
        ap.error(str(e))
    ox = None
    if args.engine != 'rdflib':
        try:
            import pyoxigraph as ox
        except ImportError:
            if args.engine == 'oxigraph':
                ap.error('pyoxigraph is not installed; use --engine rdflib')
    engine = 'oxigraph' if ox else 'rdflib'
    g = ox.Store() if ox else rdflib.Graph(bind_namespaces='none')
    ontology = rdflib.Graph().parse(args.with_ontology) if args.with_ontology else None
    if args.entail:
        try:
            import owlrl
        except ImportError:
            ap.error('--entail needs owlrl: pip install owlrl')
        closure = rdflib.Graph(bind_namespaces='none')
        for f in files:
            closure.parse(f, format='turtle')
        if ontology is not None:
            closure += ontology
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False,
                               datatype_axioms=False).expand(closure)
        # OWL-RL derives generalised triples (a literal as subject); RDF stores reject them
        for t in [t for t in closure if isinstance(t[0], rdflib.Literal)]:
            closure.remove(t)
        if ox:
            g.load(closure.serialize(format='nt'), format=ox.RdfFormat.N_TRIPLES)
        else:
            g = closure
    else:
        for f in files:
            if ox:
                g.load(path=str(f), format=ox.RdfFormat.TURTLE)
            else:
                g.parse(f, format='turtle')
        if ontology is not None:
            if ox:
                g.load(ontology.serialize(format='nt'), format=ox.RdfFormat.N_TRIPLES)
            else:
                g += ontology
    def query(q):
        if ox:
            return g.query(q)
        from rdflib.plugins.sparql import prepareQuery
        return g.query(prepareQuery(q, initNs={}))
    def val(v):
        return None if v is None else (v.value if ox else str(v))
    type_counts = {val(r[0]): int(val(r[1])) for r in query('SELECT ?t (COUNT(*) AS ?n) WHERE { ?s a ?t } GROUP BY ?t')}
    results = []
    def save_report():
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(dict(
                engine=engine, files=[str(f) for f in files], triples=len(g), parameters=params,
                type_counts=type_counts, results=results), indent=2) + '\n')
    for cq, title, body in queries:
        start = time.monotonic()
        item = dict(cq=cq, title=title)
        try:
            rows = query(body)
            item['variables'] = [v.value if ox else str(v) for v in (rows.variables if ox else rows.vars)]
            sample = []; count = 0
            for row in rows:
                count += 1
                if len(sample) < max(0, args.rows):
                    sample.append([val(v) for v in row])
            item.update(count=count, sample=sample, status='answered' if count else 'empty')
            # prerequisite records are those of the named-entity CQ file (CQ ids are per file)
            required = REQUIRES.get(cq) if args.cqs.name == 'named_entity_ontology_CQs.md' else None
            if not count and required and not type_counts.get(N + required):
                item.update(status='unavailable', reason=f'No {required} records in loaded data; this feature/profile is absent.')
            if not count and cq == 'CQ41':
                item.update(status='empty', reason='No matching judge activity with a judge: label; generic validation activities are not per-judge records.')
        except Exception as e:
            item.update(status='error', error=f'{type(e).__name__}: {e}')
        item['seconds'] = round(time.monotonic() - start, 3)
        results.append(item)
        save_report()
        print(f"{cq:5} {item['status']:11} {item.get('count', 0):8} {title}", flush=True)
        if 'error' in item:
            print('      ' + item['error'])
        for row in item.get('sample', []):
            print('      ' + ' | '.join('' if v is None else v[:100] for v in row))
    failures = sum(x['status'] == 'error' for x in results)
    print(f"{len(results)} queries, {failures} errors; {engine}; {len(files)} files, {len(g)} triples")
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
