"""
Deterministic PubMed query builder.

Compiles concept blocks + inclusion/exclusion criteria into a single Boolean
PubMed query string. Given the same inputs it always produces byte-identical
output, and we hash that output so a search is fully reproducible.

Structure produced (PICO-style):
    (block_1) AND (block_2) AND ... AND (filters) NOT (exclusions)

Each concept block is an OR of:
    - MeSH headings    -> "Heading"[MeSH Terms]        (PubMed auto-explodes)
                          "Heading"[MeSH:NoExp]         (if explosion disabled)
    - free-text terms  -> "term"[Title/Abstract]        (catches not-yet-indexed
                                                          and preprint-style records)

MeSH [MeSH Terms] already explodes indexed articles; the free-text entry terms
(from the exploded descendant descriptors + domain vocab + user synonyms) are
what push recall onto records MeSH has not indexed yet. That combination is the
"exhaustive" part.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class Concept:
    name: str
    mesh: list[str] = field(default_factory=list)       # MeSH headings
    freetext: list[str] = field(default_factory=list)   # Title/Abstract terms
    explode: bool = True                                 # explode MeSH headings

    @staticmethod
    def from_dict(d: dict) -> "Concept":
        return Concept(
            name=d.get("name", ""),
            mesh=list(d.get("mesh", [])),
            freetext=list(d.get("freetext", [])),
            explode=bool(d.get("explode", True)),
        )


@dataclass
class Filters:
    date_from: str = ""            # "YYYY" or "YYYY/MM/DD"
    date_to: str = ""
    languages: list[str] = field(default_factory=list)          # [la]
    article_types_include: list[str] = field(default_factory=list)  # [pt]
    article_types_exclude: list[str] = field(default_factory=list)  # NOT [pt]
    species: str = ""              # "humans" | "animals" | ""
    exclude_terms: list[str] = field(default_factory=list)      # NOT [Title/Abstract]
    custom_include: str = ""       # raw PubMed fragment, ANDed
    custom_exclude: str = ""       # raw PubMed fragment, NOTed

    @staticmethod
    def from_dict(d: dict) -> "Filters":
        d = d or {}
        return Filters(
            date_from=str(d.get("date_from", "")).strip(),
            date_to=str(d.get("date_to", "")).strip(),
            languages=list(d.get("languages", [])),
            article_types_include=list(d.get("article_types_include", [])),
            article_types_exclude=list(d.get("article_types_exclude", [])),
            species=str(d.get("species", "")).strip().lower(),
            exclude_terms=list(d.get("exclude_terms", [])),
            custom_include=str(d.get("custom_include", "")).strip(),
            custom_exclude=str(d.get("custom_exclude", "")).strip(),
        )


def _q(term: str) -> str:
    """Quote a term for PubMed, escaping embedded quotes."""
    return '"' + term.replace('"', "").strip() + '"'


def _dedupe(seq) -> list[str]:
    seen = set()
    out = []
    for s in seq:
        k = s.strip().lower()
        if s.strip() and k not in seen:
            seen.add(k)
            out.append(s.strip())
    return out


def build_block(concept: Concept) -> str:
    parts: list[str] = []
    tag = "[MeSH Terms]" if concept.explode else "[MeSH:NoExp]"
    for h in _dedupe(concept.mesh):
        parts.append(f"{_q(h)}{tag}")
    for t in _dedupe(concept.freetext):
        parts.append(f"{_q(t)}[Title/Abstract]")
    if not parts:
        return ""
    return "(" + " OR ".join(parts) + ")"


def _date_clause(f: Filters) -> str:
    lo = f.date_from or "1000"
    hi = f.date_to or "3000"
    return f'("{lo}"[Date - Publication] : "{hi}"[Date - Publication])'


def build_query(concepts: list[Concept], filters: Filters) -> str:
    blocks = [b for b in (build_block(c) for c in concepts) if b]
    if not blocks:
        raise ValueError("No searchable concept blocks (add MeSH headings or free-text terms).")

    query = " AND ".join(blocks)

    incl: list[str] = []
    if filters.date_from or filters.date_to:
        incl.append(_date_clause(filters))
    langs = _dedupe(filters.languages)
    if langs:
        incl.append("(" + " OR ".join(f"{_q(l)}[Language]" for l in langs) + ")")
    pts = _dedupe(filters.article_types_include)
    if pts:
        incl.append("(" + " OR ".join(f"{_q(p)}[Publication Type]" for p in pts) + ")")
    if filters.species in ("humans", "human"):
        incl.append('"Humans"[MeSH Terms]')
    elif filters.species in ("animals", "animal"):
        incl.append('("Animals"[MeSH Terms] NOT "Humans"[MeSH Terms])')
    if filters.custom_include:
        incl.append(f"({filters.custom_include})")

    for clause in incl:
        query = f"({query}) AND {clause}"

    excl: list[str] = []
    ex_pts = _dedupe(filters.article_types_exclude)
    if ex_pts:
        excl.append("(" + " OR ".join(f"{_q(p)}[Publication Type]" for p in ex_pts) + ")")
    ex_terms = _dedupe(filters.exclude_terms)
    if ex_terms:
        excl.append("(" + " OR ".join(f"{_q(t)}[Title/Abstract]" for t in ex_terms) + ")")
    if filters.custom_exclude:
        excl.append(f"({filters.custom_exclude})")

    for clause in excl:
        query = f"({query}) NOT {clause}"

    return query


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def compile_search(concepts_in: list[dict], filters_in: dict) -> dict:
    concepts = [Concept.from_dict(c) for c in concepts_in]
    filters = Filters.from_dict(filters_in)
    query = build_query(concepts, filters)
    return {
        "query": query,
        "hash": query_hash(query),
        "n_concepts": len([c for c in concepts if c.mesh or c.freetext]),
        "n_terms": sum(len(_dedupe(c.mesh)) + len(_dedupe(c.freetext)) for c in concepts),
    }
