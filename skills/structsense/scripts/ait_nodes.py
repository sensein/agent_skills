"""AIT taxonomy reader: the nodes of an Allen taxonomy, read from the trusted ontologies.

`prompts/extractor-cell-type-ait-mapping.md` Pass 3 picks a taxonomy with
`scripts/ait_taxonomy.py`, then finds the node inside it; Pass 4 needs that node's
marker genes. Both come from here, never from model knowledge.

The node data are the parsed Allen taxonomies in `trusted_ontologies/` (the directory
`concept_mapping.json` → `trusted_ontologies.directory` names, as for every other
trusted ontology). Which file holds which taxonomy and species is the catalog entry's
`trusted_files` in the manifest `trusted_ontologies/ait_taxonomies.json`; the files are instances of the BICAN
Cell Taxonomy model, `trusted_ontologies/cell_taxonomy.ttl` (the catalog's
`trusted_model`). Read through that model:

- `bican:CellTypeTaxonomy` — one per file (`bican:title`, `bican:reference_genome`);
- `bican:CellTypeSet` — the hierarchy levels, ordered by `bican:order`
  (e.g. Neighborhood 0 / Class 1 / Subclass 2 / Group 3 / cluster_id 4);
- `bican:CellTypeTaxon` — a node: `rdfs:label`, `bican:accession_id`,
  `bican:part_of_set` (its level), `bican:has_parent`, `bican:curated_markers`,
  `bican:cell_type_ontology_term` / `skos:broadMatch` (CL), `bican:literature_name_*`,
  `bican:literature_support`, and `bican:has_abbreviation`;
- `bican:Abbreviation` — a token of a node label: `bican:term` (`GATA3`, `SN`),
  `bican:meaning`, `bican:entity_type` (`gene` | `anatomical` | `cell_type`) and
  `skos:broadMatch` (UBERON / DHBA / CL).

`node_id` (written to `mappings.ait_node_id`) is the node's `accession_id`; a node
without one (the cluster level) uses its `rdfs:label`, which is unique per file.

Marker genes of a node, in this order and de-duplicated: its `curated_markers`, then
the `gene` abbreviations of its own label, then (unless `own_only`) those of its
ancestors, nearest first. `curated_markers` are links to `bican:GeneAnnotation`
instances (the model's range); their symbols (`biolink:symbol`) are read from the
genome annotation the taxonomy file itself names (`CellTypeTaxonomy.
gene_annotation_version`, e.g. `ENSEMBL-9606-109` -> `annotation-ENSEMBL-9606-109.ttl`
in `trusted_ontologies/`, bkbit gff2jsonld output) — loaded only when markers are asked
for. A marker IRI the annotation file does not hold is listed in
`Taxonomy.unresolved_markers`, never guessed.

A paper's genes are linked the other way round: `GeneLookup` resolves a symbol to the
`GeneAnnotation` IRI in the species' trusted annotation (catalog `gene_annotations`,
NCBI taxon id or species name -> file), by exact symbol, then the same symbol in another case, then synonym,
and only when exactly one gene matches. `scripts/ait_ttl.py` uses it for every gene
entity of an AIT run.

    python -m scripts.ait_nodes info AIT19.5
    python -m scripts.ait_nodes search AIT19.5 --label "D1 MSN" --gene DRD1 --region putamen
    python -m scripts.ait_nodes show AIT19.5 CS20250428_GROUP_0033
    python -m scripts.ait_nodes markers AIT19.5 CS20250428_GROUP_0033 --out markers.json
    python -m scripts.ait_nodes genes human DRD1 TAC1     # GeneAnnotation IRIs (catalog gene_annotations)

Needs rdflib (requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scripts import ait_taxonomy

SKILL_DIR = Path(__file__).resolve().parents[1]
BICAN = "https://identifiers.org/brain-bican/vocab/"
SKOS_BROAD = "http://www.w3.org/2004/02/skos/core#broadMatch"


@dataclass
class Abbreviation:
    term: str
    meaning: str
    entity_type: str
    broad_match: List[str] = field(default_factory=list)


@dataclass
class Node:
    node_id: str
    iri: str
    label: str
    level: str
    level_order: Optional[int]
    accession_id: str = ""
    parent_id: str = ""
    ancestors: List[str] = field(default_factory=list)  # nearest first
    n_children: int = 0
    curated_markers: List[str] = field(default_factory=list)  # GeneAnnotation IRIs
    abbreviations: List[Abbreviation] = field(default_factory=list)
    cl_terms: List[str] = field(default_factory=list)
    literature_name_short: str = ""
    literature_name_long: str = ""
    literature_support: str = ""

    def genes(self) -> List[str]:
        return [a.term for a in self.abbreviations if a.entity_type == "gene"]

    def regions(self) -> List[Abbreviation]:
        return [a for a in self.abbreviations if a.entity_type == "anatomical"]


@dataclass
class Taxonomy:
    ait_id: str
    species: str
    title: str
    file: str
    reference_genome: str
    levels: List[Tuple[str, int, int]]  # (label, order, n_nodes)
    nodes: Dict[str, Node]
    gene_file: str = ""  # genome annotation the curated_markers link into

    def node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    def gene_symbols(self) -> Dict[str, str]:
        """{GeneAnnotation IRI: symbol} from `gene_file`; empty when there is none."""
        if not self.gene_file:
            return {}
        return _gene_symbols(str(trusted_dir() / self.gene_file))

    def curated_symbols(self, node_id: str) -> List[str]:
        sym = self.gene_symbols()
        return sorted({sym[i] for i in self.nodes[node_id].curated_markers if i in sym})

    def unresolved_markers(self) -> List[str]:
        """curated_markers IRIs the gene annotation file does not hold."""
        sym = self.gene_symbols()
        return sorted({i for n in self.nodes.values() for i in n.curated_markers if i not in sym})

    def markers(self, node_id: str, own_only: bool = False) -> List[str]:
        n = self.nodes[node_id]
        out = (self.curated_symbols(node_id) if n.curated_markers else []) + n.genes()
        if not own_only:
            for a in n.ancestors:
                out += self.nodes[a].genes()
        return list(dict.fromkeys(out))


def trusted_dir() -> Path:
    """`trusted_ontologies.directory` from concept_mapping.json (default `trusted_ontologies`)."""
    return ait_taxonomy.trusted_dir()


def resolve(identifier: str, species: Optional[str] = None,
            catalog: Optional[Path] = None) -> Tuple[dict, str, str]:
    """(catalog entry, species, file name) for an AIT id / CCN / taxonomy_name.

    A per-species AIT number (AIT19.5) implies its species; otherwise `species` is
    required when the taxonomy has more than one trusted file. Raises LookupError when
    the taxonomy is unknown or has no trusted file for that species.
    """
    entry = ait_taxonomy.find(identifier, catalog)
    if entry is None:
        raise LookupError(f"{identifier!r} is not in trusted_ontologies/ait_taxonomies.json")
    files = {k.lower(): v for k, v in (entry.get("trusted_files") or {}).items()}
    if not files:
        raise LookupError(f"{entry['title']}: no trusted taxonomy file (trusted_files) — "
                          "nodes cannot be looked up locally")
    by_species = {v.lower(): k.lower() for k, v in (entry.get("ait_ids_by_species") or {}).items()}
    sp = (species or by_species.get(identifier.strip().lower()) or "").lower()
    if not sp and len(files) == 1:
        sp = next(iter(files))
    if sp not in files:
        raise LookupError(f"{entry['title']}: no trusted file for species {sp or '(unspecified)'!r}; "
                          f"available: {', '.join(sorted(files))}")
    return entry, sp, files[sp]


@lru_cache(maxsize=None)
def _parse(path: str):
    import rdflib  # requirements.txt; imported here so the catalog stays stdlib-only
    return rdflib.Graph().parse(path, format="turtle")


@lru_cache(maxsize=None)
def _gene_symbols(path: str) -> Dict[str, str]:
    from rdflib import RDF, URIRef
    if not Path(path).exists():
        raise LookupError(f"gene annotation file missing: {path}")
    g = _parse(path)
    sym = URIRef("https://w3id.org/biolink/vocab/symbol")
    return {str(s): str(g.value(s, sym)) for s in g.subjects(RDF.type, URIRef(BICAN + "GeneAnnotation"))
            if g.value(s, sym) is not None}


@dataclass
class GeneMatch:
    symbol: str  # as asked
    iri: str = ""  # GeneAnnotation IRI, empty when unresolved
    gene_symbol: str = ""  # the annotation's own biolink:symbol
    tier: str = ""  # symbol | symbol_case | synonym
    reason: str = ""  # why unresolved
    file: str = ""


class GeneLookup:
    """Symbol -> GeneAnnotation in one species' genome annotation (gff2jsonld output).

    Tiers, strongest first: `symbol` (exact), `symbol_case` (same symbol in another
    case, e.g. mouse-style `Pou6f2` for POU6F2), `synonym` (exact). A tier must name
    exactly one gene; an ambiguous tier is reported, never broken by picking one. The
    same rule bkbit's ait2jsonld uses for curated_markers.
    """

    def __init__(self, file: str):
        from rdflib import RDF, URIRef
        self.file = file
        g = _parse(str(trusted_dir() / file))
        bl = "https://w3id.org/biolink/vocab/"
        sym, syn = URIRef(bl + "symbol"), URIRef(bl + "synonym")
        self.symbol_of: Dict[str, str] = {}
        self.by_symbol: Dict[str, set] = {}
        self.by_folded: Dict[str, set] = {}
        self.by_synonym: Dict[str, set] = {}
        for s in g.subjects(RDF.type, URIRef(BICAN + "GeneAnnotation")):
            iri = str(s)
            for o in g.objects(s, sym):
                self.symbol_of.setdefault(iri, str(o))
                self.by_symbol.setdefault(str(o), set()).add(iri)
                self.by_folded.setdefault(str(o).casefold(), set()).add(iri)
            for o in g.objects(s, syn):
                self.by_synonym.setdefault(str(o), set()).add(iri)

    def resolve(self, symbol: str) -> GeneMatch:
        s = symbol.strip()
        for tier, ids in (("symbol", self.by_symbol.get(s)), ("symbol_case", self.by_folded.get(s.casefold())),
                          ("synonym", self.by_synonym.get(s))):
            if not ids:
                continue
            if len(ids) > 1:
                return GeneMatch(symbol, reason=f"ambiguous: {len(ids)} genes match by {tier}", file=self.file)
            iri = next(iter(ids))
            return GeneMatch(symbol, iri, self.symbol_of.get(iri, ""), tier, file=self.file)
        return GeneMatch(symbol, reason="no gene with this symbol or synonym", file=self.file)


def gene_annotation_file(species: str = "", ncbi_taxon: str = "", catalog: Optional[Path] = None) -> str:
    """The trusted genome annotation for a species, or ''.

    Catalog `gene_annotations` is keyed by NCBI taxon id (`9606`); `ncbi_taxon` wins,
    then `species` is matched against each entry's common and scientific names
    (case-insensitive), so "human", "Homo sapiens" and 9606 all find the human file."""
    entries = ait_taxonomy.load_catalog(catalog).get("gene_annotations") or {}
    tx = re.sub(r"\D", "", str(ncbi_taxon or ""))
    if tx and tx in entries:
        return entries[tx]["file"]
    name = " ".join((species or "").lower().split())
    for e in entries.values():
        if name and name in e.get("names", []):
            return e["file"]
    return ""


@lru_cache(maxsize=None)
def gene_lookup(species: str = "", ncbi_taxon: str = "") -> Optional[GeneLookup]:
    """GeneLookup for a species (name or NCBI taxon id), or None when no trusted annotation covers it."""
    f = gene_annotation_file(species, ncbi_taxon)
    return _lookup_for_file(f) if f else None


@lru_cache(maxsize=None)
def _lookup_for_file(f: str) -> GeneLookup:
    return GeneLookup(f)


# a gene match tier -> the SKOS relation it supports (identity only on the exact symbol)
GENE_TIER_TO_SKOS = {"symbol": "exactMatch", "symbol_case": "closeMatch", "synonym": "closeMatch"}


def paper_species(entities: Sequence[Dict[str, str]]) -> Tuple[str, str]:
    """(name, ncbi_taxon_id) of an AIT paper's one experimental species, else ('', '')."""
    exp = {(r.get("label_verbatim", ""), r.get("ncbi_taxon_id", "")) for r in entities
           if r.get("entity_type") == "species" and r.get("species_role") == "experimental"}
    return next(iter(exp)) if len(exp) == 1 else ("", "")


def resolve_gene_row(row: Dict[str, str], default_species: Tuple[str, str] = ("", ""),
                     symbol: Optional[str] = None) -> GeneMatch:
    """The GeneAnnotation of one `entities.csv` gene row, in its species' trusted annotation.

    The species is the row's `ncbi_taxon_id`, else its `species` name (common or
    scientific), else `default_species` (the paper's one experimental species).
    `symbol` overrides the row's `label_normalized` (a reviewer's remap query)."""
    sym = (symbol or row.get("label_normalized") or row.get("label_verbatim") or "").strip()
    name, taxon = (row.get("species") or "").strip(), (row.get("ncbi_taxon_id") or "").strip()
    if not name and not taxon:
        name, taxon = default_species
    lk = gene_lookup("", taxon) if taxon else None
    lk = lk or (gene_lookup(name) if name else None)
    if lk is None:
        sp = name or (f"NCBITaxon:{taxon}" if taxon else "")
        return GeneMatch(sym, reason=f"no trusted gene annotation for species {sp or '(none given)'!r}")
    return lk.resolve(sym)


def gene_concept_fields(gm: GeneMatch) -> Dict[str, str]:
    """A GeneMatch as the concept-mapping fields every mode records (tool provenance)."""
    if not gm.iri:
        return {"concept_mapping_provenance": "unmapped", "notes": gm.reason,
                "mapping_sources_tried": f"trusted:{Path(gm.file).stem}" if gm.file else "trusted:gene_annotations"}
    return {"ontology_id": gm.iri, "ontology_label": gm.gene_symbol, "ontology": "BKBIT",
            "mapping_source": f"trusted:{Path(gm.file).stem}", "match_tier": GENE_TIER_TO_SKOS[gm.tier],
            "ontology_match_type": f"gene {gm.tier}", "alignment_method": "trusted_ontology",
            "concept_mapping_provenance": "tool",
            "notes": "" if gm.tier == "symbol" else
            f"{gm.symbol!r} matched {gm.gene_symbol!r} by {gm.tier}, not exact symbol: review"}


def _gene_file_of(tax, g, B, directory: Optional[Path] = None) -> str:
    """The genome annotation a taxonomy file's curated_markers link into: the file names
    it (`bican:gene_annotation_version`, written by bkbit ait2jsonld --gene_annotation)."""
    v = str(g.value(tax, B("gene_annotation_version")) or "").strip() if tax is not None else ""
    if not v:
        return ""
    f = f"annotation-{v}.ttl"
    return f if ((directory or trusted_dir()) / f).exists() else ""


def load(identifier: str, species: Optional[str] = None, *, catalog: Optional[Path] = None,
         directory: Optional[Path] = None) -> Taxonomy:
    from rdflib import RDF, RDFS, URIRef

    entry, sp, fname = resolve(identifier, species, catalog)
    path = (directory or trusted_dir()) / fname
    if not path.exists():
        raise LookupError(f"trusted taxonomy file missing: {path}")
    g = _parse(str(path))
    B = lambda local: URIRef(BICAN + local)  # noqa: E731

    def lit(s, p) -> str:
        v = g.value(s, p)
        return "" if v is None else str(v)

    tax = next(g.subjects(RDF.type, B("CellTypeTaxonomy")), None)
    sets = {}
    for s in g.subjects(RDF.type, B("CellTypeSet")):
        order = g.value(s, B("order"))
        sets[s] = (lit(s, RDFS.label), int(order) if order is not None else None)

    abbrevs = {}
    for a in g.subjects(RDF.type, B("Abbreviation")):
        abbrevs[a] = Abbreviation(term=lit(a, B("term")), meaning=lit(a, B("meaning")),
                                  entity_type=lit(a, B("entity_type")),
                                  broad_match=sorted(str(o) for o in g.objects(a, URIRef(SKOS_BROAD))))

    by_iri: Dict[object, Node] = {}
    for s in g.subjects(RDF.type, B("CellTypeTaxon")):
        level, order = sets.get(g.value(s, B("part_of_set")), ("", None))
        acc = lit(s, B("accession_id"))
        cl = {str(o) for o in g.objects(s, B("cell_type_ontology_term"))}
        cl |= {str(o).rsplit("/", 1)[-1].replace("_", ":") for o in g.objects(s, URIRef(SKOS_BROAD))
               if "/obo/CL_" in str(o)}
        by_iri[s] = Node(
            node_id=acc or lit(s, RDFS.label), iri=str(s), label=lit(s, RDFS.label),
            level=level, level_order=order, accession_id=acc,
            curated_markers=sorted(str(o) for o in g.objects(s, B("curated_markers"))),
            abbreviations=sorted((abbrevs[a] for a in g.objects(s, B("has_abbreviation")) if a in abbrevs),
                                 key=lambda a: (a.entity_type, a.term)),
            cl_terms=sorted(cl),
            literature_name_short=lit(s, B("literature_name_short")),
            literature_name_long=lit(s, B("literature_name_long")),
            literature_support=lit(s, B("literature_support")))

    for s, n in by_iri.items():
        p = g.value(s, B("has_parent"))
        n.parent_id = by_iri[p].node_id if p in by_iri else ""
        seen, cur = set(), p
        while cur in by_iri and cur not in seen:
            seen.add(cur)
            n.ancestors.append(by_iri[cur].node_id)
            cur = g.value(cur, B("has_parent"))
    nodes = {n.node_id: n for n in by_iri.values()}
    for n in nodes.values():
        if n.parent_id:
            nodes[n.parent_id].n_children += 1

    levels = sorted(((lab, o if o is not None else 99, sum(1 for n in nodes.values() if n.level == lab))
                     for lab, o in sets.values()), key=lambda x: x[1])
    return Taxonomy(ait_id=ait_taxonomy.preferred_id(entry, sp), species=sp,
                    title=lit(tax, B("title")) if tax is not None else entry["taxonomy_name"],
                    file=fname, reference_genome=lit(tax, B("reference_genome")) if tax is not None else "",
                    levels=levels, nodes=nodes,
                    gene_file=_gene_file_of(tax, g, B, directory))


_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _tokens(s: str) -> set:
    return {t.lower() for t in _TOKEN.findall(s)}


def search(tax: Taxonomy, label: str = "", genes: Sequence[str] = (), regions: Sequence[str] = (),
           level: Optional[str] = None, top_n: int = 10) -> List[dict]:
    """Shortlist nodes for a paper entity. A shortlist to adjudicate, not a decision.

    +5 exact (case-insensitive) label / literature-name match; +2 per label token shared
    with the node label, its literature names or its abbreviation terms/meanings;
    +3 per paper gene in the node's marker set (curated + label genes, with ancestors);
    +2 per paper region matching an anatomical abbreviation (term or meaning) of the node
    or its ancestors. Gene symbols are compared case-insensitively here (a shortlist);
    the Pass 4 diff stays exact.
    """
    want = _tokens(label)
    genes_l = {g.lower() for g in genes}
    regions_l = [r.lower() for r in regions if r.strip()]
    out = []
    for n in tax.nodes.values():
        if level and n.level.lower() != level.lower():
            continue
        names = [n.label, n.literature_name_short, n.literature_name_long]
        vocab = set().union(*(_tokens(x) for x in names))
        for a in n.abbreviations:
            vocab |= _tokens(a.term) | _tokens(a.meaning)
        score, why = 0, []
        if label and label.strip().lower() in {x.lower() for x in names if x}:
            score += 5
            why.append("label_exact")
        shared = sorted(want & vocab)
        if shared:
            score += 2 * len(shared)
            why.append("label_tokens:" + ",".join(shared))
        mk = {m.lower() for m in tax.markers(n.node_id)} if genes_l else set()
        hit = sorted(genes_l & mk)
        if hit:
            score += 3 * len(hit)
            why.append("marker_genes:" + ",".join(hit))
        regs = [a for x in [n.node_id] + n.ancestors for a in tax.nodes[x].regions()]
        rhit = sorted({r for r in regions_l for a in regs
                       if r == a.term.lower() or r in a.meaning.lower() or a.meaning.lower() in r})
        if rhit:
            score += 2 * len(rhit)
            why.append("brain_region:" + ",".join(rhit))
        if score:
            out.append({"node_id": n.node_id, "label": n.label, "level": n.level, "score": score,
                        "basis": why, "parent": tax.nodes[n.parent_id].label if n.parent_id else ""})
    out.sort(key=lambda r: (-r["score"], r["level"], r["label"]))
    return out[:top_n]


def describe(tax: Taxonomy, node_id: str) -> dict:
    n = tax.nodes[node_id]
    d = asdict(n)
    d["path"] = [f"{tax.nodes[a].level}: {tax.nodes[a].label}" for a in reversed(n.ancestors)]
    sym = tax.gene_symbols() if n.curated_markers else {}
    d["curated_markers"] = [{"iri": i, "symbol": sym.get(i, "")} for i in n.curated_markers]
    d["markers"] = tax.markers(node_id)
    d["ait_id"], d["species"], d["file"], d["gene_file"] = tax.ait_id, tax.species, tax.file, tax.gene_file
    return d


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("taxonomy", help="AIT id, CCN or taxonomy_name (a per-species AIT id implies the species)")
        p.add_argument("--species", help="needed when the id does not imply one")

    common(sub.add_parser("info", help="taxonomy title, file, levels"))
    s = sub.add_parser("search", help="shortlist nodes by label, marker genes, region")
    common(s)
    s.add_argument("--label", default="")
    s.add_argument("--gene", action="append", default=[], help="repeatable")
    s.add_argument("--region", action="append", default=[], help="repeatable")
    s.add_argument("--level", help="restrict to one level (e.g. Subclass)")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--all", action="store_true", help="list every node (with --level, every node of it)")
    sh = sub.add_parser("show", help="one node, with its path, abbreviations and markers")
    common(sh)
    sh.add_argument("node_id")
    m = sub.add_parser("markers", help="{node_id: [genes]} for ait_gene_diff --markers")
    common(m)
    m.add_argument("node_id", nargs="+")
    m.add_argument("--own-only", action="store_true", help="do not add ancestors' label genes")
    m.add_argument("--out", help="write JSON here (default stdout)")
    gn = sub.add_parser("genes", help="GeneAnnotation IRIs for gene symbols, in a species' trusted annotation")
    gn.add_argument("species", help="a name (human, Homo sapiens, macaque, ...) or an NCBI taxon id (9606)")
    gn.add_argument("symbol", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "genes":
        sp = args.species
        lk = gene_lookup("", sp) if sp.isdigit() else gene_lookup(sp)
        if lk is None:
            print(f"error: no trusted gene annotation for species {args.species!r} (catalog gene_annotations)")
            return 1
        print(json.dumps([asdict(lk.resolve(s)) for s in args.symbol], indent=2))
        return 0

    try:
        tax = load(args.taxonomy, args.species)
    except LookupError as e:
        print(f"error: {e}")
        return 1

    if args.cmd == "info":
        print(json.dumps({"ait_id": tax.ait_id, "species": tax.species, "title": tax.title,
                          "file": tax.file, "gene_file": tax.gene_file,
                          "reference_genome": tax.reference_genome,
                          "levels": [{"level": l, "order": o, "n_nodes": c} for l, o, c in tax.levels]},
                         indent=2))
    elif args.cmd == "search":
        if args.all:
            rows = [{"node_id": n.node_id, "label": n.label, "level": n.level}
                    for n in sorted(tax.nodes.values(), key=lambda n: (n.level_order or 0, n.label))
                    if not args.level or n.level.lower() == args.level.lower()]
        else:
            rows = search(tax, args.label, args.gene, args.region, args.level, args.top)
        print(json.dumps(rows, indent=2))
    elif args.cmd == "show":
        if args.node_id not in tax.nodes:
            print(f"error: {args.node_id!r} is not a node of {tax.file}")
            return 1
        print(json.dumps(describe(tax, args.node_id), indent=2, ensure_ascii=False))
    else:
        missing = [x for x in args.node_id if x not in tax.nodes]
        if missing:
            print(f"error: not nodes of {tax.file}: {missing}")
            return 1
        data = {x: tax.markers(x, own_only=args.own_only) for x in args.node_id}
        sym = tax.gene_symbols()
        lost = sorted({i for x in args.node_id for i in tax.nodes[x].curated_markers if i not in sym})
        if lost:
            print(f"warning: {len(lost)} curated_markers not in {tax.gene_file or '(no gene_annotation_version in the taxonomy file)'}: {lost}")
        text = json.dumps(data, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {len(data)} marker sets to {args.out}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
