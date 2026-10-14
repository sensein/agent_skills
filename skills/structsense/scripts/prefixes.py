"""One prefix registry for extraction, mapping and representation.

A prefix means exactly one namespace everywhere in the skill — in an item's
`ontology` field, in `label_routing`, in the CURIE of an ner:OntologyConcept, in
the ner:ExternalOntology hub, and in the TTL's own @prefix lines. If two places
disagree (`NCBITAXON` from BioPortal, `NCBITaxon` from the IRI), one entity ends up
under two ontology hubs and a CURIE stops expanding to its IRI. So every component
asks this registry instead of deriving prefixes itself.

Entries come from, in order of authority:

  1. `default_ontology/ttl_config.json` → `obo_prefixes` (OBO PURL space,
     http://purl.obolibrary.org/obo/<PREFIX>_) and `curie_expansions`
     (other id spaces, e.g. EFO, HGNC, NCBIGene, UniProtKB);
  2. the trusted lexicon (`concept_mapping index`), which records every namespace
     a trusted file's classes live in, under that row's CURIE prefix from
     trusted_ontologes/priority.md — this is how BKE's
     https://identifiers.org/brain-bican/vocab/ becomes BKE:.

Matching is case-insensitive; the registered spelling is canonical. A prefix
claiming two namespaces, or a namespace claimed by two prefixes, is a conflict —
reported by `python -m scripts.prefixes check` and by the validator.

    python -m scripts.prefixes show | check | compact <IRI> | expand <CURIE> | canonical <prefix>
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = _SCRIPTS_DIR.parent
DEFAULT_TTL_CONFIG = SKILL_DIR / "default_ontology" / "ttl_config.json"
DEFAULT_MAPPING_CONFIG = SKILL_DIR / "concept_mapping.json"
OBO = "http://purl.obolibrary.org/obo/"


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


class PrefixRegistry:
    def __init__(self, ttl_config: Path = DEFAULT_TTL_CONFIG,
                 mapping_config: Optional[Path] = DEFAULT_MAPPING_CONFIG, *, use_lexicon: bool = True):
        raw = json.loads(Path(ttl_config).read_text())
        self.fallback_template = raw.get("curie_fallback_template")
        self._by_prefix: dict[str, tuple[str, str, str]] = {}   # lower -> (Canonical, namespace, source)
        self._namespaces: list[tuple[str, str]] = []            # (namespace, Canonical), longest first
        self.conflicts: list[str] = []
        for p in raw.get("obo_prefixes") or []:
            self._add(p, f"{OBO}{p}_", "ttl_config:obo_prefixes")
        for p, template in _clean(raw.get("curie_expansions")).items():
            if template.endswith("{id}"):
                self._add(p, template[:-len("{id}")], "ttl_config:curie_expansions")
        # namespace families: one rule registers every member on first use
        # (BioPortal's http://purl.bioontology.org/ontology/<ACRONYM>/ -> ACRONYM)
        self._patterns = [(re.compile(x["regex"]), int(x.get("prefix_group", 1)))
                          for x in raw.get("namespace_patterns") or [] if isinstance(x, dict) and x.get("regex")]
        if use_lexicon and mapping_config and Path(mapping_config).is_file():
            self._load_lexicon(Path(mapping_config))
        self._namespaces.sort(key=lambda x: -len(x[0]))

    def _add(self, prefix: str, namespace: str, source: str) -> None:
        low = prefix.lower()
        have = self._by_prefix.get(low)
        if have:
            if have[1] != namespace:
                # OBO prefixes own <PREFIX>_; a second namespace for the same prefix is
                # the case this registry exists to catch.
                self.conflicts.append(f"prefix {prefix!r}: {have[1]} ({have[2]}) vs {namespace} ({source})")
            return
        for ns, p in self._namespaces:
            if ns == namespace and p.lower() != low:
                self.conflicts.append(f"namespace {namespace}: prefix {p!r} vs {prefix!r} ({source})")
                return
        self._by_prefix[low] = (prefix, namespace, source)
        self._namespaces.append((namespace, prefix))

    def _load_lexicon(self, mapping_config: Path) -> None:
        cfg = json.loads(mapping_config.read_text())
        tcfg = cfg.get("trusted_ontologies") or {}
        lex = Path(tcfg.get("lexicon_dir", "trusted_ontologes/lexicon"))
        db = (lex if lex.is_absolute() else SKILL_DIR / lex) / "lexicon.sqlite"
        if not db.is_file():
            return
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute("SELECT source, prefix, namespace, classes FROM namespaces "
                               "WHERE role = 'primary' ORDER BY classes DESC").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        # Only a file's PRIMARY namespace(s) carry its own prefix; its OBO and other
        # known namespaces were already registered from ttl_config, and anything else
        # it contains is unregistered (concept_mapping.Lexicon._build_one).
        for src, prefix, ns, _n in rows:
            self._add(prefix, ns, f"trusted:{src}")

    # -- queries -------------------------------------------------------------
    def canonical(self, prefix: Optional[str], iri: Optional[str] = None) -> Optional[str]:
        """Canonical spelling of a prefix; with `iri`, a prefix known only through a
        namespace pattern (first seen in this IRI) is registered and returned."""
        if not prefix:
            return None
        hit = self._by_prefix.get(prefix.lower())
        if not hit and iri:
            self.compact(iri)
            hit = self._by_prefix.get(prefix.lower())
        return hit[0] if hit else None

    def namespace(self, prefix: str) -> Optional[str]:
        hit = self._by_prefix.get(prefix.lower())
        return hit[1] if hit else None

    def canonical_iri(self, iri: str) -> str:
        """One term, one IRI. A BioPortal PURL for an ontology whose prefix is already
        registered elsewhere (http://purl.bioontology.org/ontology/NCBITAXON/9443 while
        NCBITaxon is the OBO namespace) is rewritten to that namespace, so the same
        term from two mapping sources is the same concept node."""
        if iri and iri.startswith("https://purl.obolibrary.org/obo/"):
            iri = "http://" + iri[len("https://"):]  # OBO PURLs are canonically http
        m = re.match(r"^http://purl\.bioontology\.org/ontology/([A-Za-z][A-Za-z0-9_.-]*)/(.+)$", iri or "")
        if m:
            hit = self._by_prefix.get(m.group(1).lower())
            if hit and not hit[1].startswith("http://purl.bioontology.org/"):
                return hit[1] + m.group(2)
        return iri

    def compact(self, iri: str) -> Optional[tuple[str, str]]:
        """IRI -> (CURIE, canonical prefix), or None if no registered namespace holds it."""
        iri = self.canonical_iri(iri)
        for ns, prefix in self._namespaces:
            if iri.startswith(ns) and len(iri) > len(ns):
                return f"{prefix}:{iri[len(ns):]}", prefix
        for rx, grp in self._patterns:
            m = rx.match(iri)
            if not m or len(iri) <= m.end():
                continue
            prefix, ns = m.group(grp), m.group(0)
            if self._by_prefix.get(prefix.lower()):
                return None  # the prefix already names another namespace: never two for one
            self._add(prefix, ns, "ttl_config:namespace_patterns")
            self._namespaces.sort(key=lambda x: -len(x[0]))
            return f"{prefix}:{iri[m.end():]}", prefix
        return None

    def expand(self, curie: str) -> Optional[str]:
        if ":" not in curie:
            return None
        prefix, local = curie.split(":", 1)
        ns = self.namespace(prefix)
        if ns:
            return ns + local
        if self.fallback_template:
            return self.fallback_template.format(prefix=prefix, id=local)
        return None

    def bindable(self) -> dict[str, str]:
        """Non-OBO prefixes for @prefix lines (OBO terms share the single obo: prefix)."""
        return {p: ns for p, ns, _ in self._by_prefix.values() if not ns.startswith(OBO)}

    def entries(self) -> list[tuple[str, str, str]]:
        return sorted(self._by_prefix.values())


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["show", "check", "compact", "expand", "canonical"])
    ap.add_argument("value", nargs="?")
    args = ap.parse_args()
    reg = PrefixRegistry()
    if args.cmd == "show":
        for p, ns, src in reg.entries():
            print(f"{p:<14} {ns:<60} {src}")
    elif args.cmd == "check":
        for c in reg.conflicts:
            print(f"CONFLICT {c}")
        print(f"{len(reg.entries())} prefixes, {len(reg.conflicts)} conflict(s)")
        return 1 if reg.conflicts else 0
    elif args.cmd == "compact":
        print(reg.compact(args.value or ""))
    elif args.cmd == "expand":
        print(reg.expand(args.value or ""))
    else:
        print(reg.canonical(args.value))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
