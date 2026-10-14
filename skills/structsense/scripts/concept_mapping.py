"""Concept mapping driven by configuration: trusted ontologies first, by priority,
then the remote tools (local hybrid, BioPortal) for whatever they did not resolve.

Everything is read from `concept_mapping.json` (skill root; `--config` for another)
and from the editable priority table `trusted_ontologes/priority.md`:

  - which sources run and in what order (`sources_priority`, default
    trusted -> local_hybrid -> bioportal)
  - which trusted ontology files are used, and their priority (priority.md — the
    ONLY place; edit it, no rebuild needed)
  - which annotation properties count as a match (`match_properties`)
  - which id prefixes an extractor label may map into (`label_routing`, a filter)
  - what skos tier each kind of match implies (`match_type_tiers`)

Nothing about a particular ontology is coded here.

Sources, consulted in order; an item goes to the next source only if every earlier
one left it unmapped, and an unavailable source is skipped, not fatal:

  trusted       the files listed in priority.md. `index` streams each file ONCE into
                a flat lexicon `lexicon/<name>.tsv.gz` (key, IRI, prefix, match
                type, label — inspectable, diffable) and loads all of them into one
                indexed `lexicon.sqlite`, so a lookup is a single query however
                large the ontologies are (PR alone is 1.2 GB of OWL). Only files whose
                size/mtime changed are re-indexed. Priority is applied at QUERY time,
                so reordering priority.md never needs a rebuild. Matching is exact on
                a normalised form (case, dashes, whitespace, Greek letters, CamelCase;
                optional singular), never fuzzy. Two different classes tying for the
                best match is AMBIGUOUS: not mapped, candidates recorded, falls through.
  local_hybrid  the self-hosted BM25+dense service (health-checked).
  bioportal     BioPortal search (needs BIOPORTAL_API_KEY) — the fallback when no
                trusted ontology and no local service has the term.
  ols           EBI OLS4 (opt-in; no gene coverage).

Every mapping made here is a tool mapping (`concept_mapping_provenance: "tool"`,
rule 15) and says where it came from: `alignment_method` ("trusted_ontology" or
"direct_tool_call"), `mapping_source` ("trusted:cl", "local_hybrid", "bioportal"),
`ontology_match_type`, and `match_tier` (from `match_type_tiers`; json_to_ttl uses
it when no judge has set a tier).

The same lexicon gives the normalizedEntityKey its canonical form
(`TrustedMapper.canonical_label`): the preferred label of the one class an entity's
text denotes, so papers writing "SST-INs" and "somatostatin interneurons" share a key.

Usage:
    python -m scripts.concept_mapping index [--only NAME ...] [--rebuild]
    python -m scripts.concept_mapping show
    python -m scripts.concept_mapping lookup "SST interneuron" --label CellType [--all]
    python -m scripts.concept_mapping map work/paper_final.json [--only-unmapped] [--sources trusted,bioportal]
    python -m scripts.concept_mapping export-synonyms [out.tsv.gz]   # variant key -> canonical key
    python -m scripts.concept_mapping init-priorities [--force]      # seed priority.md from the directory
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

SKILL_DIR = _SCRIPTS_DIR.parent
DEFAULT_CONFIG = SKILL_DIR / "concept_mapping.json"
LEXICON_FORMAT = 5

logger = logging.getLogger("concept_mapping")

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDF_DESC = f"{{{_RDF}}}Description"
_ABOUT = f"{{{_RDF}}}about"
_RESOURCE = f"{{{_RDF}}}resource"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_RDF_ID = f"{{{_RDF}}}ID"
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
          "κ": "kappa", "λ": "lambda", "μ": "mu", "θ": "theta", "ω": "omega"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Optional[Path] = None) -> dict:
    p = Path(path or DEFAULT_CONFIG)
    cfg = json.loads(p.read_text())
    cfg["_path"] = str(p.resolve())
    return cfg


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def _resolve(p: str) -> Path:
    q = Path(os.path.expanduser(p))
    return q if q.is_absolute() else SKILL_DIR / q


def read_priority_table(path: Path) -> list[dict]:
    """Parse priority.md: the first markdown table with Priority and File columns.
    A row whose priority is not a number (e.g. `off`) is disabled."""
    rows: list[dict] = []
    header: Optional[list[str]] = None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            low = [c.lower() for c in cells]
            if "priority" in low and "file" in low:
                header = low
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(header, cells))
        prio = row.get("priority", "").strip().lower()
        if not prio.isdigit() or not row.get("file"):
            continue
        prefix = (row.get("curie prefix") or "").strip()
        labels = [x.strip() for x in (row.get("labels") or "*").split(",") if x.strip()]
        spaces = [x.strip() for x in (row.get("namespace") or "").split(",") if x.strip() not in ("", "-")]
        rows.append({"name": row.get("name") or Path(row["file"]).stem, "file": row["file"],
                     "priority": int(prio), "namespaces": spaces,
                     "curie_prefix": None if prefix in ("", "-") else prefix,
                     "labels": None if not labels or "*" in labels else labels,
                     "notes": row.get("notes")})
    if header is None:
        raise ValueError(f"{path}: no markdown table with Priority and File columns")
    names = [r["name"] for r in rows]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate Name(s) {sorted(dupes)}")
    return sorted(rows, key=lambda r: r["priority"])


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def norm(text: str) -> str:
    s = "".join(_GREEK.get(ch, ch) for ch in unicodedata.normalize("NFKC", text or ""))
    s = "".join(" " if ord(ch) > 127 and not ch.isalnum() else ch for ch in s)  # en dash etc. separate words
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def split_camel(text: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)


def query_variants(term: str, singularize: bool, camel: bool = True) -> list[tuple[str, int]]:
    """(normalised query, variant rank). Rank 0 = as written, 1 = singularised."""
    out: list[tuple[str, int]] = []
    for f in [norm(term)] + ([norm(split_camel(term))] if camel else []):
        if f and f not in [o[0] for o in out]:
            out.append((f, 0))
    if singularize:
        for base, _ in list(out):
            words = base.split()
            last = words[-1]
            cands = []
            if len(last) > 4 and last.endswith("ies"):
                cands.append(last[:-3] + "y")
            if len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is")):
                cands.append(last[:-1])
            for c in cands:
                v = " ".join(words[:-1] + [c])
                if v not in [o[0] for o in out]:
                    out.append((v, 1))
    return out


def namespace_of(iri: str) -> str:
    """The namespace a term IRI lives in: the OBO `<PREFIX>_` stem, else everything up
    to the last '#' or '/'."""
    m = re.match(r"^(https?://purl\.obolibrary\.org/obo/[A-Za-z][A-Za-z0-9]*_)", iri)
    if m:
        return m.group(1)
    if "#" in iri:  # everything after the first '#' is the local name, slashes included
        return iri[:iri.index("#") + 1]
    cut = iri.rfind("/")
    return iri[:cut + 1] if cut > 0 else iri


def obo_prefix(iri: str) -> Optional[str]:
    m = re.match(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9]*)_", iri)
    if m:
        return m.group(1)
    m = re.match(r"^https?://www\.ebi\.ac\.uk/efo/(EFO)_", iri)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Lexicon build: ontology file -> rows
# ---------------------------------------------------------------------------

class LexiconSource:
    """One row of priority.md, and how to read its file."""

    def __init__(self, spec: dict, tcfg: dict):
        self.spec = spec
        self.name = spec["name"]
        self.priority = int(spec["priority"])
        self.path = _resolve(tcfg.get("directory", "trusted_ontologes")) / spec["file"]
        self.curie_prefix = spec.get("curie_prefix")
        self.labels_scope = set(spec["labels"]) if spec.get("labels") else None
        self.props = {mt: set(v) for mt, v in _clean(tcfg.get("match_properties")).items()}
        self.entity_types = set(tcfg.get("entity_types") or ["http://www.w3.org/2002/07/owl#Class"])
        self.deprecated_prop = tcfg.get("deprecated_property")
        self.include_deprecated = bool(tcfg.get("include_deprecated", False))
        self.camel = bool(tcfg.get("split_camel_case", True))
        self.local_names = bool(tcfg.get("index_local_name_when_unlabeled", False))
        self.static_sig = ""  # set by Lexicon: the known-namespace registry this index was built against

    @property
    def own_prefix(self) -> str:
        return self.curie_prefix or self.name.upper()

    def admits(self, label: Optional[str]) -> bool:
        return self.labels_scope is None or (label or "") in self.labels_scope

    def signature(self) -> str:
        st = self.path.stat()
        return json.dumps([LEXICON_FORMAT, st.st_size, int(st.st_mtime), sorted(self.entity_types),
                           sorted(self.spec.get("namespaces") or []), self.static_sig,
                           sorted((k, sorted(v)) for k, v in self.props.items()),
                           self.include_deprecated, self.camel, self.curie_prefix, self.local_names])

    def _match_type_of(self, prop_iri: str) -> Optional[str]:
        for mt, iris in self.props.items():
            if prop_iri in iris:
                return mt
        return None

    def classes(self) -> Iterator[tuple[str, dict[str, list[str]]]]:
        if self.path.suffix.lower() in (".owl", ".rdf", ".xml"):
            try:
                yield from self._iter_rdfxml()
                return
            except ET.ParseError as e:
                logger.warning("%s: not streamable RDF/XML (%s); falling back to rdflib", self.path.name, e)
        yield from self._iter_rdflib()

    def _iter_rdfxml(self):
        """Stream RDF/XML so a 1 GB OBO file indexes in bounded memory. Relative
        rdf:about / rdf:ID are resolved against xml:base, as an RDF parser would."""
        from urllib.parse import urljoin
        depth = 0
        # never the file's own path: a local path is not a term IRI. A row's declared
        # Namespace (priority.md) is the base for a file that states none itself.
        declared = self.spec.get("namespaces") or []
        base = declared[0] if declared else None
        for event, el in ET.iterparse(str(self.path), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1 and el.get(_XML_BASE) and not declared:
                    base = el.get(_XML_BASE)
                continue
            depth -= 1
            if depth == 1 and el.tag.endswith("}Ontology") and base is None:
                onto = el.get(_ABOUT) or ""
                if re.match(r"^https?://", onto):
                    base = onto
            if base is None and depth == 1 and ((el.get(_ABOUT) or "").startswith("#") or el.get(_RDF_ID)):
                el.clear()
                continue
            if depth == 1 and (el.get(_ABOUT) is not None or el.get(_RDF_ID)):
                about = el.get(_ABOUT)
                subject = urljoin(base or "", about) if about is not None \
                    else (base or "").split("#")[0] + "#" + el.get(_RDF_ID)
                if about is not None and about.startswith("#"):
                    subject = base.split("#")[0] + about
                if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", subject or ""):
                    el.clear()
                    continue  # not an absolute IRI
                tag = el.tag[1:].replace("}", "", 1) if el.tag.startswith("{") else el.tag
                typed = tag in self.entity_types or (el.tag == _RDF_DESC and any(
                    ch.tag == f"{{{_RDF}}}type" and ch.get(_RESOURCE, "") in self.entity_types for ch in el))
                if typed:
                    ann: dict[str, list[str]] = defaultdict(list)
                    deprecated = False
                    for ch in el:
                        prop = ch.tag[1:].replace("}", "", 1) if ch.tag.startswith("{") else ch.tag
                        if self.deprecated_prop and prop == self.deprecated_prop:
                            deprecated = (ch.text or "").strip().lower() == "true"
                            continue
                        mt = self._match_type_of(prop)
                        lang = ch.get(_XML_LANG)
                        if mt and ch.text and ch.text.strip() and (not lang or lang.lower().startswith("en")):
                            ann[mt].append(ch.text.strip())
                    if (ann or self.local_names) and (self.include_deprecated or not deprecated):
                        yield subject, ann
            if depth <= 1:
                el.clear()

    def _iter_rdflib(self):
        from rdflib import Graph, Literal, URIRef
        from rdflib.namespace import RDF
        g = Graph()
        g.parse(str(self.path))
        subjects = set()
        for et in self.entity_types:
            subjects |= {s for s in g.subjects(RDF.type, URIRef(et)) if isinstance(s, URIRef)}
        for c in subjects:
            ann: dict[str, list[str]] = defaultdict(list)
            deprecated = False
            for p, o in g.predicate_objects(c):
                if self.deprecated_prop and str(p) == self.deprecated_prop:
                    deprecated = str(o).lower() == "true"
                mt = self._match_type_of(str(p))
                if mt and isinstance(o, Literal) and (not o.language or o.language.startswith("en")):
                    ann[mt].append(str(o))
            if (ann or self.local_names) and (self.include_deprecated or not deprecated):
                yield str(c), ann

    def rows(self) -> Iterator[tuple[str, str, str, str, str]]:
        """(key, iri, prefix, match_type, preferred label)."""
        for iri, ann in self.classes():
            local = iri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            if not ann.get("label") and self.local_names and not re.fullmatch(r"[A-Za-z]*[_:]?\d+", local):
                # opaque ids (CL_0000540, 12345) are not names; readable local names are
                ann = {**ann, "local_name": [local.replace("_", " ")]}
            if not ann:
                continue
            label = (ann.get("label") or [None])[0] or split_camel(local.replace("_", " "))
            prefix = ""  # assigned in Lexicon.build by the prefix rules
            seen = set()
            for mt, values in ann.items():
                for v in values:
                    forms = {norm(v)} | ({norm(split_camel(v))} if self.camel else set())
                    for k in forms:
                        if k and (k, mt) not in seen:
                            seen.add((k, mt))
                            yield k, iri, prefix, mt, label


# ---------------------------------------------------------------------------
# Lexicon store: TSV per ontology + one SQLite index
# ---------------------------------------------------------------------------

class Lexicon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        tcfg = cfg.get("trusted_ontologies") or {}
        self.priority_file = _resolve(tcfg.get("priority_file", "trusted_ontologes/priority.md"))
        self.dir = _resolve(tcfg.get("lexicon_dir", "trusted_ontologes/lexicon"))
        self.db_path = self.dir / "lexicon.sqlite"
        self.sources = [LexiconSource(s, tcfg) for s in read_priority_table(self.priority_file)]
        from prefixes import PrefixRegistry
        # Known namespaces (OBO + ttl_config curie_expansions) decide a term's prefix
        # before any file's own prefix can claim it.
        self.known = PrefixRegistry(use_lexicon=False)
        import hashlib
        sig = hashlib.sha1(json.dumps(self.known.entries()).encode()).hexdigest()[:12]
        for src in self.sources:
            src.static_sig = sig
        self._db: Optional[sqlite3.Connection] = None

    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.db_path))
            ver = self._db.execute("PRAGMA user_version").fetchone()[0]
            if ver != LEXICON_FORMAT:
                # an index built by another version of this script: rebuild from scratch
                self._db.executescript("DROP TABLE IF EXISTS terms; DROP TABLE IF EXISTS sources; "
                                       "DROP TABLE IF EXISTS namespaces;")
                self._db.execute(f"PRAGMA user_version = {LEXICON_FORMAT}")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, file TEXT, signature TEXT,
                    classes INTEGER, rows INTEGER);
                CREATE TABLE IF NOT EXISTS terms (source TEXT, key TEXT, iri TEXT, prefix TEXT,
                    match_type TEXT, label TEXT);
                CREATE INDEX IF NOT EXISTS terms_key ON terms(key);
                CREATE TABLE IF NOT EXISTS namespaces (source TEXT, prefix TEXT, namespace TEXT,
                    classes INTEGER, role TEXT);
            """)
        return self._db

    def status(self) -> dict[str, dict]:
        cur = self.db().execute("SELECT name, file, signature, classes, rows FROM sources")
        return {r[0]: {"file": r[1], "signature": r[2], "classes": r[3], "rows": r[4]} for r in cur}

    def stale(self) -> list[LexiconSource]:
        have = self.status()
        return [s for s in self.sources
                if s.path.is_file() and have.get(s.name, {}).get("signature") != s.signature()]

    def missing_files(self) -> list[LexiconSource]:
        return [s for s in self.sources if not s.path.is_file()]

    def build(self, only: Optional[list[str]] = None, rebuild: bool = False,
              progress: Callable[[str], None] = lambda m: None) -> dict:
        todo = [s for s in self.sources if s.path.is_file() and (only is None or s.name in only)]
        if not rebuild:
            stale = {s.name for s in self.stale()}
            todo = [s for s in todo if s.name in stale]
        done: dict[str, dict] = {}
        db = self.db()
        for s in todo:
            progress(f"indexing {s.name} ({s.path.name}, {s.path.stat().st_size / 1e6:.1f} MB)")
            try:
                done[s.name] = self._build_one(s)
            except Exception as e:  # one unreadable file must not lose the others
                db.rollback()
                logger.warning("%s: could not be indexed: %s: %s", s.name, type(e).__name__, e)
                done[s.name] = {"error": f"{type(e).__name__}: {e}"}
                continue
            r = done[s.name]
            progress(f"  {s.name}: {r['classes']} classes, {r['rows']} lexicon rows; primary namespace(s) "
                     f"{r['primary']} as {s.own_prefix}:"
                     + (f"; {r['unregistered_classes']} class(es) in {len(r['unregistered'])} unregistered "
                        f"namespace(s) not used for mapping" if r["unregistered"] else ""))
        names = {s.name for s in self.sources}
        for gone in set(self.status()) - names:  # dropped from priority.md
            db.execute("DELETE FROM terms WHERE source = ?", (gone,))
            db.execute("DELETE FROM namespaces WHERE source = ?", (gone,))
            db.execute("DELETE FROM sources WHERE name = ?", (gone,))
        db.commit()
        return done

    def _build_one(self, s: "LexiconSource") -> dict:
        """Parse one file, assign every term's prefix by the prefix rules, export TSV.

        Prefix rules, in order (scripts/prefixes.py is the registry):
          1. OBO PURL                     -> its own prefix (CL_... -> CL)
          2. a KNOWN namespace            -> that prefix (ttl_config curie_expansions),
                                             whatever file the class came from
          3. the file's primary namespace -> the row's CURIE prefix (priority.md
                                             Namespace column, else the dominant one)
          4. anything else                -> unregistered: kept, never used for mapping
        """
        db = self.db()
        for table in ("terms", "namespaces"):
            db.execute(f"DELETE FROM {table} WHERE source = ?", (s.name,))
        db.execute("DELETE FROM sources WHERE name = ?", (s.name,))
        ns_classes: dict[str, set] = defaultdict(set)
        known_prefix: dict[str, Optional[str]] = {}
        batch: list[tuple] = []
        n_rows = 0
        for key, iri, _p, mt, label in s.rows():
            ns = namespace_of(iri)
            if ns not in known_prefix:
                hit = obo_prefix(iri)
                if not hit:
                    c = self.known.compact(iri)
                    hit = c[1] if c else None
                known_prefix[ns] = hit
            ns_classes[ns].add(iri)
            batch.append((s.name, key, iri, known_prefix[ns] or "?", mt, label))
            n_rows += 1
            if len(batch) >= 50000:
                db.executemany("INSERT INTO terms VALUES (?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO terms VALUES (?,?,?,?,?,?)", batch)
        pending = {ns: len(v) for ns, v in ns_classes.items() if not known_prefix.get(ns)}
        declared = s.spec.get("namespaces") or []
        if not s.curie_prefix and not declared:
            # An OBO file (CURIE prefix '-') has no prefix of its own to give away:
            # its stray non-OBO namespaces stay unregistered rather than become 'CL:'.
            primary: list[str] = []
        elif declared:
            primary = [ns for ns in pending if any(ns.startswith(d) for d in declared)]
        else:
            primary = [max(pending, key=pending.get)] if pending else []
        # a sub-namespace (vocab/Region#) is covered by its parent: register only the parent,
        # but claim its terms for this prefix too
        covered = [ns for ns in pending if any(ns != p and ns.startswith(p) for p in primary)]
        primary = [p for p in primary if not any(p != q and p.startswith(q) for q in primary)]
        for ns in primary + covered:
            db.execute("UPDATE terms SET prefix = ? WHERE source = ? AND prefix = '?' AND iri LIKE ? ESCAPE '\\'",
                       (s.own_prefix, s.name, ns.replace("%", "\\%").replace("_", "\\_") + "%"))
        unregistered = {ns: n for ns, n in pending.items() if ns not in primary and ns not in covered}
        db.execute("UPDATE terms SET prefix = NULL WHERE source = ? AND prefix = '?'", (s.name,))
        roles = []
        for ns, iris in ns_classes.items():
            if known_prefix.get(ns):
                roles.append((s.name, known_prefix[ns], ns, len(iris), "known"))
            elif ns in primary:
                roles.append((s.name, s.own_prefix, ns, len(iris), "primary"))
            elif ns in covered:
                roles.append((s.name, s.own_prefix, ns, len(iris), "covered"))
            else:
                roles.append((s.name, None, ns, len(iris), "unregistered"))
        db.executemany("INSERT INTO namespaces VALUES (?,?,?,?,?)", roles)
        n_classes = sum(len(v) for v in ns_classes.values())
        db.execute("INSERT INTO sources VALUES (?,?,?,?,?)", (s.name, s.spec["file"], s.signature(), n_classes, n_rows))
        db.commit()
        tsv = self.dir / f"{s.name}.tsv.gz"
        with gzip.open(tsv, "wt", newline="") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n")
            w.writerow(["key", "iri", "prefix", "match_type", "label"])
            for row in db.execute("SELECT key, iri, prefix, match_type, label FROM terms WHERE source = ?", (s.name,)):
                w.writerow(row)
        return {"classes": n_classes, "rows": n_rows, "primary": primary,
                "unregistered": sorted(unregistered, key=unregistered.get, reverse=True)[:10],
                "unregistered_classes": sum(unregistered.values())}

    def ensure_built(self) -> None:
        stale = self.stale()
        if stale:
            logger.warning("trusted lexicon is stale for %s — indexing now (one-time; run "
                           "`python -m scripts.concept_mapping index` ahead of time to avoid this)",
                           ", ".join(s.name for s in stale))
            self.build(only=[s.name for s in stale], progress=logger.info)

    def query(self, keys: list[str]) -> list[tuple]:
        if not keys:
            return []
        q = ",".join("?" * len(keys))
        return self.db().execute(
            f"SELECT source, key, iri, prefix, match_type, label FROM terms "
            f"WHERE key IN ({q}) AND prefix IS NOT NULL",
            keys).fetchall()


# ---------------------------------------------------------------------------
# Trusted mapper
# ---------------------------------------------------------------------------

_REG = None


def _registry():
    global _REG
    if _REG is None:
        from prefixes import PrefixRegistry
        _REG = PrefixRegistry()
    return _REG


class TrustedMapper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        tcfg = cfg.get("trusted_ontologies") or {}
        self.match_on = list(tcfg.get("match_on") or ["label", "exact_synonym"])
        # weaker synonym types, used ONLY when no identity-type match exists anywhere
        # (GO lists "synaptic transmission" as a broad synonym of "chemical synaptic
        # transmission"); their skos tier comes from match_type_tiers, never exactMatch
        self.fallback_on = [m for m in tcfg.get("fallback_match_on") or [] if m not in self.match_on]
        self.strategy = tcfg.get("strategy", "priority")
        self.singularize = bool(tcfg.get("singularize_query", True))
        self.camel = bool(tcfg.get("split_camel_case", True))
        guard = tcfg.get("abbreviation_guard") or {}
        self.abbrev_len = int(guard.get("max_length", 0))
        self.abbrev_needs_route = bool(guard.get("require_label_route", True))
        # IRIs that are in a trusted file but are not domain concepts (schema enum
        # values, e.g. BKE AbbreviationEntityType#gene): never a mapping target
        self.exclude = [re.compile(x) for x in tcfg.get("exclude_iri_patterns") or [] if not x.startswith("_")]
        routing = cfg.get("label_routing") or {}
        self.always = {p.upper() for p in routing.get("always_allowed_prefixes") or []}
        self.routing = {k: [p.upper() for p in v] for k, v in _clean(routing).items()
                        if k != "always_allowed_prefixes"}
        self.tiers = _clean(cfg.get("match_type_tiers"))
        self.lexicon = Lexicon(cfg)
        self.by_name = {s.name: s for s in self.lexicon.sources}
        self._memo: dict[tuple[str, Optional[str]], dict] = {}
        self._ready = False

    def available(self) -> list[LexiconSource]:
        if not self._ready:
            for s in self.lexicon.missing_files():
                logger.warning("trusted ontology %s: %s does not exist — skipped", s.name, s.path)
            self.lexicon.ensure_built()
            self._ready = True
        return [s for s in self.lexicon.sources if s.path.is_file()]

    def route(self, label: Optional[str]) -> Optional[list[str]]:
        return self.routing.get(label or "")

    def candidates(self, term: str, label: Optional[str], weak: bool = False) -> list[tuple]:
        """Ranked [(rank, iri, match_type, source, prefix, label)] — best first."""
        if not self.available():
            return []
        route = self.route(label)
        if self.abbrev_len and route is None and self.abbrev_needs_route \
                and len(re.sub(r"[^A-Za-z0-9]", "", term)) <= self.abbrev_len:
            return []  # abbreviation guard: a bare short token needs a label route
        variants = query_variants(term, self.singularize, self.camel)
        vrank = dict(variants)
        found = []
        for src_name, key, iri, prefix, mt, lab in self.lexicon.query([k for k, _ in variants]):
            src = self.by_name.get(src_name)
            if src is None or not src.admits(label) or (mt not in self.match_on and not (weak and mt in self.fallback_on)):
                continue
            if any(x.search(iri) for x in self.exclude):
                continue
            p = prefix.upper()
            if route is not None and p not in route and p not in self.always:
                continue
            route_rank = route.index(p) if route and p in route else 0
            order = self.match_on + self.fallback_on
            mrank = order.index(mt)
            if self.strategy == "best_match":
                rank = (mrank, vrank[key], src.priority, route_rank)
            else:  # priority.md decides first; the route orders prefixes inside one file
                rank = (src.priority, route_rank, mrank, vrank[key])
            found.append((rank, iri, mt, src, prefix, lab))
        found.sort(key=lambda f: f[0])
        strong = [f for f in found if f[2] in self.match_on]
        return strong if strong else found  # a weak synonym only when nothing names it exactly

    def lookup(self, term: str, label: Optional[str] = None, weak: bool = False) -> dict:
        """weak=True: the last-resort pass over broad/narrow/related synonyms, run only
        after every source (remote included) left the item unmapped, and never for a
        bare abbreviation ("Ala" is a related synonym of the wrong CHEBI class)."""
        if weak and self.abbrev_len and len(re.sub(r"[^A-Za-z0-9]", "", term)) <= self.abbrev_len:
            return {"term": term, "concept_mapping_provenance": "unmapped"}
        memo_key = (term, label, weak)
        if memo_key not in self._memo:
            self._memo[memo_key] = self._decide(term, self.candidates(term, label, weak=weak))
        return self._memo[memo_key]

    def _decide(self, term: str, found: list) -> dict:
        if not found:
            return {"term": term, "concept_mapping_provenance": "unmapped"}
        best = found[0][0]
        top: dict[str, tuple] = {}
        for f in found:
            if f[0] == best:
                top.setdefault(f[1], f)
        if len(top) > 1:
            return {"term": term, "concept_mapping_provenance": "unmapped",
                    "trusted_ambiguous": [{"ontology_id": iri, "ontology_label": f[5], "ontology": f[4],
                                           "match_type": f[2], "source": f"trusted:{f[3].name}"}
                                          for iri, f in list(top.items())[:10]]}
        _, iri, mt, src, prefix, lab = found[0]
        return {
            "term": term, "ontology_id": iri, "ontology_label": lab, "ontology": prefix,
            "concept_mapping_provenance": "tool", "alignment_method": "trusted_ontology",
            "mapping_source": f"trusted:{src.name}", "ontology_match_type": mt,
            "match_tier": self.tiers.get(mt),
        }

    def canonical_label(self, term: str, label: Optional[str] = None,
                        match_types: tuple[str, ...] = ("label", "exact_synonym", "symbol")) -> Optional[str]:
        """Preferred label of the ONE class `term` denotes, or None (no hit / ambiguous).
        Identity-strength match types only: a key must never merge a term into its
        parent or into a merely related class."""
        found = [f for f in self.candidates(term, label) if f[2] in match_types]
        if not found:
            return None
        best = found[0][0]
        if len({f[1] for f in found if f[0] == best}) > 1:
            return None
        return found[0][5]


# ---------------------------------------------------------------------------
# Cascade over sources
# ---------------------------------------------------------------------------

_MAPPING_FIELDS = ("ontology_id", "ontology_label", "ontology", "concept_mapping_provenance",
                   "alignment_method", "mapping_source", "ontology_match_type", "match_tier",
                   "trusted_ambiguous", "score")


class ConceptMapper:
    """Map items through `sources_priority`. The pipeline and the CLI both use this."""

    def __init__(self, cfg: Optional[dict] = None, *, sources: Optional[list[str]] = None,
                 local_url: Optional[str] = None,
                 ask_user: Optional[Callable[[str], Optional[str]]] = None):
        self.cfg = cfg or load_config()
        self.sources = list(sources or self.cfg.get("sources_priority") or ["trusted"])
        remote = self.cfg.get("remote") or {}
        self.local_url = local_url or os.environ.get("LOCAL_CONCEPT_MAPPING_URL") \
            or remote.get("local_hybrid_url", "http://localhost:8000")
        self.max_results = int(remote.get("max_results", 1))
        self.ask_user = ask_user
        self.trusted = TrustedMapper(self.cfg) if "trusted" in self.sources else None
        self._remote: dict[str, Any] = {}
        self.history: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)

    def remote_route(self, label: Optional[str]) -> Optional[list[str]]:
        """Ontologies a remote search may return for this label: remote.label_ontologies
        first (remote-only routes, e.g. Measurement -> STATO/PATO/OBI), then the
        label_routing used for trusted files, then remote.default_ontologies. Never an
        unrestricted search when a default list is configured."""
        remote = self.cfg.get("remote") or {}
        own = _clean(remote.get("label_ontologies")).get(label or "")
        if own:
            return own
        route = self.trusted.route(label) if self.trusted else _clean(self.cfg.get("label_routing")).get(label or "")
        absent = {x.upper() for x in remote.get("not_in_bioportal") or []}
        kept = [p for p in route or [] if p.upper() not in absent]
        if kept:
            return kept
        if route is not None and not kept and label:  # an explicit [] route: never map this label
            return []
        return remote.get("default_ontologies") or None

    @staticmethod
    def representable(iri: str) -> bool:
        """A remote hit is usable only if the TTL can name it: its namespace is in the
        prefix registry (a guessed prefix is how one namespace gets two names)."""
        return bool(iri) and _registry().compact(iri) is not None

    def _remote_client(self, name: str):
        if name in self._remote:
            return self._remote[name]
        client = None
        try:
            if name == "local_hybrid":
                from local_hybrid_map import LocalHybridMapper
                url = self.local_url
                c = LocalHybridMapper(base_url=url)
                if not c.health() and self.ask_user:
                    alt = self.ask_user(f"local_hybrid not reachable at {url}. Alternate URL "
                                        f"(e.g. http://localhost:9000), or Enter to skip:")
                    if alt and alt.strip():
                        url = alt.strip().rstrip("/")
                        c = LocalHybridMapper(base_url=url)
                if c.health():
                    client, self.local_url = c, url
                self.history.append(f"local_hybrid@{url}:{'ok' if client else 'unreachable'}")
            elif name == "bioportal":
                if os.environ.get("BIOPORTAL_API_KEY"):
                    from bioportal_map import BioPortalMapper
                    client = BioPortalMapper()
                self.history.append(f"bioportal:{'ok' if client else 'skipped, BIOPORTAL_API_KEY not set'}")
            elif name == "ols":
                from ols_map import OlsMapper
                client = OlsMapper()
                self.history.append("ols:ok")
            else:
                raise ValueError(f"unknown mapping source {name!r} in sources_priority")
        except ImportError as e:
            self.history.append(f"{name}:import failed ({e})")
        self._remote[name] = client
        return client

    def usable_sources(self) -> list[str]:
        out = []
        for s in self.sources:
            if s == "trusted":
                if self.trusted and self.trusted.available():
                    out.append(s)
            elif self._remote_client(s) is not None:
                out.append(s)
        return out

    def map_items(self, items: list[dict], surface_key: str, *, only_unmapped: bool = False) -> None:
        """Map items in place. Unresolved items end as concept_mapping_provenance 'unmapped'."""
        pending = [it for it in items if it.get(surface_key)
                   and not (only_unmapped and it.get("concept_mapping_provenance") == "tool")]
        # curated overrides first (concept_mapping.json curated_mappings): terms every
        # ontology file gets wrong the same way, e.g. NCBITaxon files "mice" under the
        # genus Mus. Label-scoped, user-editable, recorded as mapping_source "curated".
        curated = {k.lower(): v for k, v in _clean(self.cfg.get("curated_mappings")).items()}
        if curated:
            still = []
            for it in pending:
                hit = curated.get(f"{_q(it, surface_key).strip().lower()}|{(it.get('label') or '').lower()}") \
                    or curated.get(_q(it, surface_key).strip().lower())
                if isinstance(hit, dict) and hit.get("ontology_id"):
                    self._apply(it, {**hit, "concept_mapping_provenance": "tool",
                                     "alignment_method": "curated_mapping", "mapping_source": "curated",
                                     "match_tier": hit.get("match_tier", "exactMatch")})
                    self.counts["curated"] += 1
                else:
                    still.append(it)
            pending = still
        for source in self.sources:
            if not pending:
                break
            if source == "trusted":
                if not (self.trusted and self.trusted.available()):
                    self.history.append("trusted:no ontology files available")
                    continue
                self.history.append(f"trusted:{len(self.trusted.available())} ontologies")
                still = []
                for it in pending:
                    m = self.trusted.lookup(_q(it, surface_key), it.get("label"))
                    if m.get("concept_mapping_provenance") == "tool":
                        self._apply(it, m)
                        self.counts["trusted"] += 1
                    else:
                        if m.get("trusted_ambiguous"):
                            it["trusted_ambiguous"] = m["trusted_ambiguous"]
                        still.append(it)
                pending = still
                continue
            client = self._remote_client(source)
            if client is None:
                continue  # unavailable: the next source (e.g. BioPortal) still gets the items
            by_label: dict[Optional[str], list[dict]] = defaultdict(list)
            for it in pending:
                by_label[it.get("label")].append(it)
            still = []
            for label, group in by_label.items():
                route = self.remote_route(label)
                if route == []:  # a general-domain label (Person, Date, ...): no ontology target
                    still.extend(group)
                    continue
                uniq = list(dict.fromkeys(_q(it, surface_key) for it in group))
                # a bare abbreviation ("DS", "PV") means nothing to a remote search
                # without the paper's expansion: DS -> Dravet syndrome. Leave it to the
                # trusted route / kg_plan instead of guessing.
                specific = bool(_clean((self.cfg.get("remote") or {}).get("label_ontologies")).get(label or "")
                                or (self.trusted.route(label) if self.trusted else None))
                if self.trusted and self.trusted.abbrev_len and not specific:
                    short = [t for t in uniq if len(re.sub(r"[^A-Za-z0-9]", "", t)) <= self.trusted.abbrev_len]
                    if short:
                        still.extend(it for it in group if _q(it, surface_key) in short)
                        group = [it for it in group if _q(it, surface_key) not in short]
                        uniq = [t for t in uniq if t not in short]
                    if not uniq:
                        continue
                try:
                    kw = {"accept": self.representable} if source == "bioportal" else {}
                    results = client.map_batch(uniq, ontologies=route, max_results=self.max_results, **kw)
                except Exception as e:  # one failing source must not lose the batch
                    self.history.append(f"{source}:error {type(e).__name__}")
                    still.extend(group)
                    continue
                by_term = dict(zip(uniq, results))
                for it in group:
                    m = by_term.get(_q(it, surface_key)) or {}
                    if m.get("remote_error"):
                        it["mapping_remote_error"] = f"{source}: {m['remote_error']}"
                    if m.get("concept_mapping_provenance") == "tool" and m.get("ontology_id"):
                        self._apply(it, {**m, "alignment_method": "direct_tool_call",
                                         "mapping_source": source})
                        self.counts[source] += 1
                    else:
                        still.append(it)
            pending = still
        if pending and self.trusted and self.trusted.fallback_on and self.trusted.available():
            still = []
            for it in pending:  # last resort: a weaker synonym, honestly tiered (never exactMatch)
                m = self.trusted.lookup(_q(it, surface_key), it.get("label"), weak=True)
                if m.get("concept_mapping_provenance") == "tool":
                    self._apply(it, m)
                    self.counts["trusted_weak"] += 1
                else:
                    still.append(it)
            pending = still
        for it in pending:
            for f in ("ontology_id", "ontology_label", "ontology"):
                it[f] = None
            it["concept_mapping_provenance"] = "unmapped"
            # the truthful reason for THIS run, replacing any stale one
            # (e.g. a pre-mapping "validation_failed" stamp)
            # an unreachable source is not evidence of absence: say so, so a later
            # `batch retry --from-stage map` can fill it in
            it["alignment_method"] = "remote_error" if it.get("mapping_remote_error") else "no_match"
            it["mapping_sources_tried"] = list(self.sources)
            self.counts["unmapped"] += 1

    @staticmethod
    def _apply(it: dict, m: dict) -> None:
        for f in _MAPPING_FIELDS:
            if m.get(f) is not None:
                it[f] = m[f]
        oid = it.get("ontology_id")
        if isinstance(oid, str) and oid.startswith("http"):
            it["ontology_id"] = _registry().canonical_iri(oid)  # one term, one IRI across sources
        it.pop("mapping_tier", None)  # a new mapping has not been judged yet

    def meta(self) -> dict:
        used = [s for s in self.sources if self.counts.get(s)]
        trusted = []
        if self.trusted:
            st = self.trusted.lexicon.status()
            trusted = [{"name": s.name, "file": s.spec["file"], "priority": s.priority,
                        "classes": st.get(s.name, {}).get("classes")}
                       for s in self.trusted.lexicon.sources if s.path.is_file()]
        return {"mapper_used": "+".join(used) or None,
                "mapper_url": self.local_url if "local_hybrid" in used else None,
                "sources_priority": self.sources,
                "trusted_ontologies": trusted,
                "priority_file": str(self.trusted.lexicon.priority_file) if self.trusted else None,
                "mapped_by_source": dict(self.counts),
                "cascade_history": self.history,
                "fallback_triggered": bool(used) and used != self.sources[:len(used)],
                "user_provided_url": False,
                "config": self.cfg.get("_path")}


def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_hash(cfg: dict) -> str:
    """sha256 of the mapping configuration in effect: concept_mapping.json + priority.md."""
    import hashlib
    h = hashlib.sha256()
    for f in (cfg.get("_path"), str(_resolve((cfg.get("trusted_ontologies") or {}).get(
            "priority_file", "trusted_ontologes/priority.md")))):
        if f and Path(f).is_file():
            h.update(Path(f).read_bytes())
    return "sha256:" + h.hexdigest()


def _q(it: dict, surface_key: str) -> str:
    """What to look up: the paper's own expansion of an abbreviation when it defines
    one ("Down syndrome (DS)" -> DS is looked up as "Down syndrome"), else the surface."""
    return it.get("mapping_query") or it[surface_key]


_DEF = re.compile(r"([A-Za-z0-9][A-Za-z0-9\-–' ]{2,80}?)\s*\(\s*([A-Za-z][A-Za-z0-9\-+/]{1,11}?)s?\s*[;,)]")


def abbreviation_table(texts) -> dict[str, str]:
    """Abbreviations the document defines, as {SHORT: long form} (Schwartz & Hearst
    2003, simplified): "long form (SF)" where SF's letters occur in order in the long
    form and its first letter starts the long form's first word. Ambiguous
    definitions (two long forms for one SF) are dropped."""
    found: dict[str, set] = {}
    for t in texts:
        for m in _DEF.finditer(str(t or "")):
            lf, sf = m.group(1).strip(), m.group(2)
            # Mixed-case two-letter physical symbols such as Rs do not follow
            # initial-letter acronym rules (series resistance != resistance).
            if len(sf) <= 2 and not sf.isupper():
                continue
            if not any(ch.isupper() for ch in sf):
                continue
            words = lf.split()
            chars = [c.lower() for c in sf if c.isalnum()]
            best = None
            for k in range(len(words) - 1, max(-1, len(words) - len(chars) - 3), -1):
                cand = " ".join(words[k:])
                if cand[:1].lower() != chars[0]:
                    continue
                pos, ok = 0, True
                for c in chars:
                    pos = cand.lower().find(c, pos)
                    if pos < 0:
                        ok = False
                        break
                    pos += 1
                if ok:
                    best = cand
                    break
            if best and best.lower() != sf.lower() and len(best) > len(sf):
                found.setdefault(sf, set()).add(best.strip(" -"))
    return {sf: next(iter(lfs)) for sf, lfs in found.items() if len({x.lower() for x in lfs}) == 1}


def map_result(result: dict, mapper: ConceptMapper, *, only_unmapped: bool = False,
               texts: Optional[list] = None) -> dict:
    """`texts`: the source text, when available, so abbreviations defined in a
    sentence no extracted item carries are still found."""
    started = _utc_now()
    items = [it for key in ("entities", "key_terms") for it in result.get(key) or [] if isinstance(it, dict)]
    abbrev = abbreviation_table(list(texts or []) +
                                list(dict.fromkeys(it.get("sentence") for it in items if it.get("sentence"))))
    for it in items:
        surf = (it.get("entity") or it.get("term") or "").strip()
        base = surf[:-1] if surf.endswith("s") and surf[:-1] in abbrev else surf
        if base in abbrev and not it.get("mapping_query"):
            it["mapping_query"] = abbrev[base]  # provenance: what was actually looked up
    result.setdefault("stats", {})["abbreviations_defined"] = len(abbrev)

    for key, surf in (("entities", "entity"), ("key_terms", "term")):
        if result.get(key):
            mapper.map_items(result[key], surf, only_unmapped=only_unmapped)
    meta = mapper.meta()
    meta.update({"started_at": started, "ended_at": _utc_now(), "config_hash": config_hash(mapper.cfg)})
    result.setdefault("stats", {})["alignment"] = meta
    return result


# ---------------------------------------------------------------------------
# Generated key synonyms
# ---------------------------------------------------------------------------

def _key(text: str) -> str:
    return norm(text).replace(" ", "_")


def export_synonyms(lex: Lexicon, out: Path, match_types=("label", "exact_synonym", "symbol")) -> dict:
    """variant key -> canonical key (the preferred label's key), WITH its context:
    one row per (variant, class) giving the class IRI, its prefix, the match type and
    the source ontology. Synonyms are context-dependent — 'pfc' is prefrontal cortex
    in UBERON and 'prefollicle cell' in FBbt — so a consumer must filter by prefix (or
    use label_routing) rather than merge on the variant alone. Within one prefix, a
    variant naming two classes is ambiguous and left out."""
    rank = {s.name: s.priority for s in lex.sources}
    per: dict[tuple[str, str], dict[str, tuple]] = defaultdict(dict)   # (variant, prefix) -> iri -> row
    q = ("SELECT source, key, iri, prefix, match_type, label FROM terms "
         "WHERE prefix IS NOT NULL AND match_type IN (%s)" % ",".join("?" * len(match_types)))
    for src, key, iri, prefix, mt, label in lex.db().execute(q, match_types):
        if src not in rank or not label:
            continue
        variant, canon = key.replace(" ", "_"), _key(label)
        if variant == canon:
            continue
        cur = per[(variant, prefix)].get(iri)
        if cur is None or rank[src] < rank[cur[3]]:
            per[(variant, prefix)][iri] = (canon, mt, iri, src)
    n = n_amb = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["variant_key", "canonical_key", "prefix", "iri", "match_type", "source"])
        for (variant, prefix), classes in sorted(per.items()):
            if len(classes) > 1:
                n_amb += 1
                continue
            canon, mt, iri, src = next(iter(classes.values()))
            w.writerow([variant, canon, prefix, iri, mt, src])
            n += 1
    return {"written": n, "ambiguous_skipped": n_amb}


# ---------------------------------------------------------------------------
# Seeding priority.md
# ---------------------------------------------------------------------------

def init_priorities(directory: Path, out: Path) -> int:
    """Write a priority table listing every ontology file in `directory`.

    Order: bke* first, then cl.owl, then the `declared` list of sources.json (its own
    order), then everything else. sources.json discovery candidates, files it does not
    declare, and the knowledge graph's own core are listed `off`.
    """
    exts = (".owl", ".rdf", ".xml", ".ttl", ".nt", ".jsonld")
    files = sorted(p.name for p in directory.iterdir() if p.suffix.lower() in exts)
    declared: list[dict] = []
    candidates: dict[str, dict] = {}
    src = directory / "sources.json"
    if src.is_file():
        try:  # tolerate trailing commas; the file is hand-maintained
            data = json.loads(re.sub(r",(\s*[}\]])", r"\1", src.read_text()))
            declared = [e for e in data.get("declared") or [] if isinstance(e, dict)]
            for e in (data.get("discovery_candidates") or {}).get("entries") or []:
                if e.get("file"):
                    candidates[e["file"]] = e
        except Exception as e:
            logger.warning("sources.json unreadable (%s); ordering by file name", e)
    by_file = {e.get("file"): e for e in declared}
    order: list[tuple[str, dict]] = []
    for f in files:
        if f.startswith("bke"):
            order.append((f, {"acronym": "BKE", "note": "BICAN knowledge-extraction taxonomy. First priority."}))
    if "cl.owl" in files:
        order.append(("cl.owl", {"acronym": "CL", "source": "https://purl.obolibrary.org/obo/cl.owl",
                                 "note": "Full Cell Ontology release (with imports and symbols)."}))
    placed = {o[0] for o in order}
    for e in declared:
        if e.get("file") in files and e["file"] not in placed:
            order.append((e["file"], e))
            placed.add(e["file"])
    order += [(f, candidates.get(f) or {}) for f in files if f not in placed]
    rows, n = [], 0
    used_names: set[str] = set()
    for f, e in order:
        acr = (e.get("acronym") or re.sub(r"-sub\d+$", "", Path(f).stem)).upper()
        name = re.sub(r"[^a-z0-9]+", "_", acr.lower()).strip("_")
        if name in used_names:  # two files for one acronym (cl.owl / cl-basic.owl)
            name = re.sub(r"[^a-z0-9]+", "_", Path(f).stem.lower()).strip("_")
        used_names.add(name)
        off_reason = None
        if e.get("role") == "core":
            off_reason = "the knowledge graph's own core ontology, not a mapping vocabulary"
        elif f == "cl-basic.owl" and "cl.owl" in files:
            off_reason = "subset of cl.owl, which is enabled"
        elif f not in by_file and not f.startswith("bke") and f != "cl.owl":
            st = (candidates.get(f) or {}).get("status")
            off_reason = "discovery candidate, not reviewed" + (f" (BioPortal status: {st})" if st else "")
        if off_reason:
            prio = "off"
        else:
            n += 1
            prio = str(n)
        note = (e.get("note") or e.get("name") or "").replace("|", "/").replace("\n", " ")
        note = (note[:140] + "…") if len(note) > 140 else note
        if off_reason:
            note = f"OFF: {off_reason}. {note}".strip()
        obo = (e.get("source") or "").startswith("https://purl.obolibrary.org")
        ns_decl = "https://identifiers.org/brain-bican/vocab/" if f.startswith("bke") else "-"
        rows.append(f"| {prio} | {name} | {f} | {'-' if obo else acr} | {ns_decl} | * | {note} |")
    head = out.read_text().split("| Priority")[0] if out.is_file() else "# Trusted ontology priority\n\n"
    out.write_text(head.rstrip() + "\n\n| Priority | Name | File | CURIE prefix | Namespace | Labels | Notes |\n"
                   "|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
    return len(rows)


# ---------------------------------------------------------------------------
# README of the trusted directory
# ---------------------------------------------------------------------------

_README_BEGIN = "<!-- BEGIN generated: python -m scripts.concept_mapping readme -->"
_README_END = "<!-- END generated -->"


def write_readme(lex: Lexicon, readme: Path) -> int:
    """(Re)write the ontology list in the trusted directory's README from priority.md
    and the lexicon, between markers, so the list cannot drift from what is used."""
    st = lex.status()
    all_rows = []
    # include the disabled rows too, straight from the table
    header, raw_rows = None, []
    for line in lex.priority_file.read_text().splitlines():
        if line.startswith("| Priority"):
            header = [c.strip().lower() for c in line.strip("|").split("|")]
        elif header and line.startswith("|") and not line.startswith("|---"):
            raw_rows.append(dict(zip(header, [c.strip() for c in line.strip("|").split("|")])))
    ns_rows = {}
    try:
        for src, prefix, ns, n, role in lex.db().execute("SELECT source, prefix, namespace, classes, role FROM namespaces"):
            if role == "primary":
                ns_rows.setdefault(src, []).append(f"{prefix}: `{ns}`")
    except sqlite3.OperationalError:
        pass
    enabled = [r for r in raw_rows if r.get("priority", "").isdigit()]
    disabled = [r for r in raw_rows if not r.get("priority", "").isdigit()]
    directory = lex.sources[0].path.parent if lex.sources else lex.priority_file.parent
    lines = [_README_BEGIN, "",
             f"**{len(enabled)} enabled** for concept mapping, **{len(disabled)} disabled** "
             f"(listed in priority.md as `off`). Priority, CURIE prefix and namespace come from "
             f"[priority.md](priority.md); class counts from the last `index`.", "",
             "| Priority | Name | File | Size | Prefix / namespace | Classes indexed |",
             "|---:|---|---|---:|---|---:|"]
    for r in enabled:
        f = directory / r["file"]
        size = f"{f.stat().st_size / 1e6:.1f} MB" if f.is_file() else "missing"
        own = "; ".join(ns_rows.get(r["name"], [])) or ("OBO prefixes" if r.get("curie prefix") in ("-", "") else "—")
        classes = st.get(r["name"], {}).get("classes")
        lines.append(f"| {r['priority']} | `{r['name']}` | {r['file']} | {size} | {own} | "
                     f"{classes if classes is not None else 'not indexed'} |")
    lines += ["", "<details><summary>Disabled rows</summary>", "",
              "| Name | File | Why |", "|---|---|---|"]
    for r in disabled:
        why = (r.get("notes") or "").split(". ")[0].removeprefix("OFF: ")
        lines.append(f"| `{r.get('name')}` | {r.get('file')} | {why} |")
    lines += ["", "</details>", "", _README_END]
    block = "\n".join(lines)
    text = readme.read_text() if readme.is_file() else "# Trusted ontologies\n"
    if _README_BEGIN in text and _README_END in text:
        text = text.split(_README_BEGIN)[0] + block + text.split(_README_END, 1)[1]
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    readme.write_text(text)
    return len(raw_rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="build/refresh the trusted lexicon (TSV + SQLite)")
    p.add_argument("--only", nargs="*", help="names from priority.md")
    p.add_argument("--rebuild", action="store_true")
    p = sub.add_parser("lookup", help="look one term up in the trusted ontologies")
    p.add_argument("term")
    p.add_argument("--label")
    p.add_argument("--all", action="store_true", help="show every candidate, ranked")
    p = sub.add_parser("map", help="map every entity/key term of a result in place")
    p.add_argument("result", type=Path)
    p.add_argument("--only-unmapped", action="store_true")
    p.add_argument("--sources", help="comma list overriding sources_priority for this run")
    p.add_argument("--mapper-url")
    p.add_argument("-o", "--output", type=Path)
    p = sub.add_parser("export-synonyms", help="write the generated key-synonym table (TSV.gz)")
    p.add_argument("out", type=Path, nargs="?")
    p = sub.add_parser("init-priorities", help="seed priority.md from the files in the directory")
    p.add_argument("--force", action="store_true", help="overwrite an existing table")
    sub.add_parser("show", help="print the effective sources / ontology priority")
    sub.add_parser("readme", help="regenerate the ontology list in the trusted directory's README.md")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tcfg = cfg.get("trusted_ontologies") or {}

    if args.cmd == "init-priorities":
        out = _resolve(tcfg.get("priority_file", "trusted_ontologes/priority.md"))
        if out.is_file() and "| Priority" in out.read_text() and not args.force:
            print(f"{out} already has a table; pass --force to regenerate it", file=sys.stderr)
            return 2
        n = init_priorities(_resolve(tcfg.get("directory", "trusted_ontologes")), out)
        print(f"wrote {out}: {n} rows", file=sys.stderr)
        return 0

    tm = TrustedMapper(cfg)
    if args.cmd == "index":
        done = tm.lexicon.build(only=args.only, rebuild=args.rebuild,
                                progress=lambda m: print(m, file=sys.stderr, flush=True))
        for s in tm.lexicon.missing_files():
            print(f"MISSING {s.name}: {s.path}", file=sys.stderr)
        bad = [k for k, v in done.items() if "error" in v]
        print(f"indexed {len(done) - len(bad)} source(s)" + (f"; failed: {', '.join(bad)}" if bad else "")
              + f"; lexicon at {tm.lexicon.dir}", file=sys.stderr)
        return 0
    if args.cmd == "show":
        st = tm.lexicon.status()
        stale = {s.name for s in tm.lexicon.stale()}
        print("sources, in order:", " -> ".join(cfg.get("sources_priority") or []))
        print(f"trusted strategy: {tm.strategy}; match on: {', '.join(tm.match_on)}")
        print(f"priority table: {tm.lexicon.priority_file}")
        for s in tm.lexicon.sources:
            state = ("MISSING" if not s.path.is_file() else "not indexed" if s.name in stale
                     else f"{st.get(s.name, {}).get('classes')} classes")
            scope = "any label" if s.labels_scope is None else ", ".join(sorted(s.labels_scope))
            print(f"  {s.priority:>3}  {s.name:<16} {s.spec['file']:<28} prefix={s.curie_prefix or '-':<10} "
                  f"{scope}  [{state}]")
        if tm.always:
            print("always-allowed prefixes:", ", ".join(sorted(tm.always)))
        for label, route in tm.routing.items():
            print(f"  route {label}: {' > '.join(route)}")
        return 0
    if args.cmd == "lookup":
        if args.all:
            for rank, iri, mt, src, prefix, lab in tm.candidates(args.term, args.label):
                print(f"{rank}  {src.name:<14} {prefix:<10} {mt:<14} {iri}  {lab}")
            return 0
        print(json.dumps(tm.lookup(args.term, args.label), indent=2))
        return 0
    if args.cmd == "readme":
        readme = tm.lexicon.priority_file.parent / "README.md"
        n = write_readme(tm.lexicon, readme)
        print(f"wrote {readme}: {n} rows", file=sys.stderr)
        return 0
    if args.cmd == "export-synonyms":
        tm.available()
        out = args.out or tm.lexicon.dir / "key_synonyms.tsv.gz"
        r = export_synonyms(tm.lexicon, out)
        print(f"wrote {out}: {r['written']} variant->canonical keys ({r['ambiguous_skipped']} ambiguous skipped)",
              file=sys.stderr)
        return 0

    result = json.loads(args.result.read_text())
    mapper = ConceptMapper(cfg, sources=args.sources.split(",") if args.sources else None,
                           local_url=args.mapper_url)
    if not mapper.usable_sources():
        print("error: no mapping source is usable (no trusted ontology indexed, local hybrid "
              "unreachable, no BIOPORTAL_API_KEY). Concept mapping is mandatory and tool-only "
              "(rule 15).", file=sys.stderr)
        return 2
    map_result(result, mapper, only_unmapped=args.only_unmapped)
    dest = args.output or args.result
    dest.write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str) + "\n")
    m = result["stats"]["alignment"]
    print(f"mapped by source: {m['mapped_by_source']}", file=sys.stderr)
    print(f"cascade: {m['cascade_history']}", file=sys.stderr)
    print(f"wrote {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
