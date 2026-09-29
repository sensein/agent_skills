"""Represent one paper's extraction as Turtle instances of the default ontology.

Deterministic, no LLM call. Reads the canonical result (`<stem>_final.json`, after
judging) and, optionally, a `kg_plan.json` holding the parts that need judgment
(normalized keys, match tiers, paper-stated relations, causal claims — see
prompts/kg-plan.md and schemas/kg-plan.schema.json). Writes `<stem>.ttl` typed
against the Named Entity Ontology (default_ontology/named_entity_ontology.owl,
namespace https://brainkb.org/ner/).

What goes in (profile "full", the optional audit view):

  spine        ner:Publication -> ner:DocumentVersion (checksum, mediaType,
               sourcePath); ner:NERExtractionActivity (prov:used docv,
               prov:wasAssociatedWith every source model); ner:ExtractionSnapshot
               (prov:hadMember every entity); concept-mapping and judge activities.
  entities     one ner:NamedEntity per canonical entity / key term / resource,
               typed with the most specific class the label licenses
               (default_ontology/label_class_map.json) plus ner:NamedEntity, with
               normalizedEntityKey (the ingestion merge handle), label, mentions,
               prov:hadPrimarySource.
  mentions     one ner:EntityMention per raw occurrence: surfaceForm, document
               offsets, ner:inSentence / ner:inSection, and an
               ner:EntityAnnotationVersion recording the classification (raw label,
               class, confidence, specificity) and which model surfaced it.
  mappings     ONLY `concept_mapping_provenance: "tool"` mappings become
               ner:resolvedToConcept + a skos match at the honest tier + a
               ner:ConceptMappingDecision. Everything else gets an rdfs:comment
               naming the gap. No IRI is ever minted from model knowledge.
  judging      each judge verdict becomes a ner:ReviewDecision attributed to that
               judge (from the `judge_ensemble` block judge_combine.py writes).
  kg_plan      RO/BFO edges, skos:broader / skos:related / rdfs:seeAlso / prov:used
               between entities, and the causal module (CausalRelation + Version +
               EffectEstimate + CausalChain).

Profile "compact" is the default: entities, mentions (surface, offsets, sentence,
source version), mappings and evidence-bearing relations, without audit records.
The CLI also writes entity-focused JSON and Turtle projections.

Then gate the file:  python -m scripts.validate_ttl <stem>.ttl   (must exit 0)

Usage:
    python -m scripts.json_to_ttl paper_final.json [--kg-plan kg_plan.json]
        [--source paper.pdf] [--out paper.ttl] [--profile full|compact]
        [--paper-slug s41593-026-02429-3] [--report paper.ttl.report.json]

Scope: NER (entities + key terms) and resource results. Structured-extraction
output follows a user schema this ontology does not describe, and ABCD/HBCD mode
writes its own `abcd:` Turtle via abcd_export.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from rdflib import Graph, Literal, Namespace, URIRef
    from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS, SKOS, XSD
except ImportError:  # pragma: no cover - reported at the CLI
    sys.stderr.write("rdflib is required: pip install 'rdflib>=7,<8'\n")
    raise

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from group_by_entity import mention_groups, reading_form  # noqa: E402

SKILL_DIR = _SCRIPTS_DIR.parent
ONTOLOGY_DIR = SKILL_DIR / "default_ontology"
DEFAULT_ONTOLOGY = ONTOLOGY_DIR / "named_entity_ontology.owl"
DEFAULT_LABEL_MAP = ONTOLOGY_DIR / "label_class_map.json"
DEFAULT_SYNONYMS = ONTOLOGY_DIR / "key_synonyms.json"
DEFAULT_TTL_CONFIG = ONTOLOGY_DIR / "ttl_config.json"
SKILL_VERSION = "0.9.0"

OBO = Namespace("http://purl.obolibrary.org/obo/")

# The SKOS mapping vocabulary itself — not policy. Which tier is used when is
# policy, and lives in ttl_config.json / concept_mapping.json.
MATCH_TIERS = {
    "exactMatch": SKOS.exactMatch, "closeMatch": SKOS.closeMatch, "broadMatch": SKOS.broadMatch,
    "narrowMatch": SKOS.narrowMatch, "relatedMatch": SKOS.relatedMatch,
}
_TIER_ALIASES = {
    "exact": "exactMatch", "close": "closeMatch", "broad": "broadMatch",
    "broader": "broadMatch", "narrow": "narrowMatch", "narrower": "narrowMatch",
    "related": "relatedMatch",
}


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


class TtlConfig:
    """Representation policy from default_ontology/ttl_config.json (see its comments)."""

    def __init__(self, path: Path = DEFAULT_TTL_CONFIG):
        raw = json.loads(Path(path).read_text())
        self.raw = raw
        self.path = str(path)
        self.ner_ns = raw.get("ontology_namespace", "https://brainkb.org/ner/")
        self.default_tier = raw.get("default_match_tier", "closeMatch")
        self.generic_keys = frozenset(raw.get("generic_keys") or [])
        self.interventional = frozenset(raw.get("interventional_evidence_bases") or [])
        self.relations = {k: URIRef(v) for k, v in _clean(raw.get("relation_predicates")).items()}
        self.relations_by_id = {str(v).rsplit("/", 1)[-1]: v for v in self.relations.values()}
        self.obo_prefixes = {p.lower(): p for p in raw.get("obo_prefixes") or []}
        self.curie_expansions = _clean(raw.get("curie_expansions"))
        self.curie_fallback = raw.get("curie_fallback_template", "https://identifiers.org/{prefix}:{id}")
        self.method_by_source = _clean(raw.get("mapping_method_by_source"))
        self.relation_by_tier = _clean(raw.get("mapping_relation_by_tier"))
        self.review_status = _clean(raw.get("review_status_by_verdict"))
        sec = _clean(raw.get("secondary_resource_classes"))
        self.secondary_default = (raw.get("secondary_resource_classes") or {}).get("_default", "ResearchEntity")
        self.secondary_classes = sec
        self.media_types = _clean(raw.get("media_types"))
        self.label_max_length = int((raw.get("labels") or {}).get("max_length") or 0)
        self.source_path_mode = raw.get("source_path", "name")
        iri = raw.get("iri") or {}
        self.iri_scheme = iri.get("scheme", "uuid5")
        self.iri_base = iri.get("base", "https://brainkb.org/kb/")
        self.uuid_ns = uuid.uuid5(uuid.NAMESPACE_URL, iri.get("namespace_seed", self.iri_base))
        self.global_names = _clean(iri.get("global_names"))
        self.paper_name = iri.get("paper_name", "{kind}|{paper}|{variant}|{local}")
        self.paper_shared_name = iri.get("paper_shared_name", "{kind}|{paper}|{local}")
        self.paper_shared_kinds = frozenset(iri.get("paper_shared_kinds") or
                                            ("publication", "document_version", "sentence", "section"))
        self.kb_ns = self.iri_base


NER = Namespace(TtlConfig().ner_ns) if DEFAULT_TTL_CONFIG.is_file() else Namespace("https://brainkb.org/ner/")
DEFAULT_KB_NS = TtlConfig().iri_base if DEFAULT_TTL_CONFIG.is_file() else "https://brainkb.org/kb/"

_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "κ": "kappa", "λ": "lambda",
    "μ": "mu", "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma",
    "τ": "tau", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
}
_NO_DEPLURAL = ("ss", "us", "is", "ics", "sis", "xis")
_INVARIANT_PLURALS: set[str] = set()   # filled from key_synonyms.json "invariant_words"


# ---------------------------------------------------------------------------
# Keys and slugs
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """Unicode-fold to lowercase ASCII with single underscores (key-normalization steps 2-3)."""
    s = "".join(_GREEK.get(ch, _GREEK.get(ch.lower(), ch)) for ch in text or "")
    # non-ASCII punctuation (en/em dash, minus, slash-like) separates words; only
    # letters are transliterated. "excitation–inhibition" -> excitation_inhibition
    s = "".join(" " if ord(ch) > 127 and not ch.isalnum() else ch for ch in s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "_", s.lower())
    return s.strip("_")


def _depluralize(key: str, surface: str) -> str:
    """Trailing plural -> singular, only where the surface's last word is plain
    lowercase prose. Symbols are left alone: "Fos" is a gene, not a plural."""
    words = (surface or "").split()
    last_word = words[-1] if words else ""
    # Plain prose, or prose capitalised at a sentence start ("Neurons"). A symbol
    # with inner capitals ("GluAs", "NMDARs") is left alone.
    if not (last_word.islower() or (last_word[:1].isupper() and last_word[1:].islower())):
        return key
    parts = key.split("_")
    last = parts[-1]
    if len(last) < 5 or last in _INVARIANT_PLURALS or last.endswith(_NO_DEPLURAL):
        return key
    if last.endswith("ies") and len(last) > 5:
        parts[-1] = last[:-3] + "y"
    elif last.endswith("s"):
        parts[-1] = last[:-1]
    return "_".join(parts)


def split_camel(text: str) -> str:
    """'CellTypeTaxonomy' -> 'Cell Type Taxonomy' (schema-style labels, e.g. BKE)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)


def normalize_key(name: str, synonyms: dict[str, str]) -> str:
    """The algorithmic part of references/key-normalization.md.

    Judgment the algorithm cannot make — spelling out a referent, expanding an
    ambiguous abbreviation — belongs in kg_plan.json, which always wins. A
    CamelCase name is split first, so an ontology label 'CellTypeTaxonomy' and the
    prose 'cell type taxonomy' give the same key.
    """
    if " " not in name.strip() and re.search(r"[a-z][A-Z]", name):
        name = split_camel(name)
    k = fold(name)
    k = re.sub(r"^(the|a|an)_", "", k)
    k = synonyms.get(k, k)
    k = _depluralize(k, name)
    return synonyms.get(k, k)


def slugify(text: str, *, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(text).replace("_", "-")).strip("-")
    return s[:max_len].strip("-") or "x"


def paper_slug_for(meta: dict, fallback: str) -> str:
    doi = (meta.get("doi") or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    if doi:
        # "10.1038/s41593-026-02429-3" -> "s41593-026-02429-3": the registrant prefix
        # adds nothing a reader needs and makes every slug start the same way.
        suffix = doi.split("/", 1)[1] if "/" in doi else doi
        return slugify(suffix)
    for k in ("arxiv_id", "pmcid", "pmid"):
        if meta.get(k):
            return slugify(f"{k}-{meta[k]}")
    if meta.get("source_path"):
        return slugify(Path(str(meta["source_path"])).stem.removesuffix("_final"))
    return slugify(fallback)


def class_snake(cls: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls).lower()


# ---------------------------------------------------------------------------
# Ontology ids -> (IRI, CURIE, acronym)
# ---------------------------------------------------------------------------

_QUALIFIER = re.compile(r"\(\s*skos:(exact|close|broad|narrow|related)\w*\s*\)", re.I)


def split_ontology_ids(raw: Optional[str]) -> list[tuple[str, Optional[str]]]:
    """Split a (possibly coordinated, possibly skos-qualified) ontology_id.

    "CL:0000617 (skos:exact); -; CL:4023017 (skos:related)" -> two ids with tier
    hints. ';' separates coordinated elements, '-' marks a gap, ',' separates
    candidate ids for one element (references/cell-annotation-conventions.md).
    """
    out: list[tuple[str, Optional[str]]] = []
    for element in str(raw or "").split(";"):
        for cand in element.split(","):
            hint = None
            m = _QUALIFIER.search(cand)
            if m:
                hint = _TIER_ALIASES[m.group(1).lower()]
                cand = _QUALIFIER.sub("", cand)
            cand = cand.strip()
            if cand and cand != "-":
                out.append((cand, hint))
    return out


_SENT_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])|\n[ \t]*\n")


def sentence_around(text: str, start: int, end: int, max_chars: int = 1200) -> Optional[str]:
    """The sentence of `text` that contains [start, end) whole: bounded by sentence
    ends or blank lines, never by a single line break (PDF text wraps mid-sentence and
    hyphenates across lines: "endocan-\nnabinoids")."""
    if not (0 <= start < end <= len(text)):
        return None
    lo = max(0, start - max_chars)
    left = [m.end() for m in _SENT_END.finditer(text, lo, start)]
    a = left[-1] if left else lo
    m = _SENT_END.search(text, end, min(len(text), end + max_chars))
    b = m.start() if m else min(len(text), end + max_chars)
    sent = text[a:b].strip()
    return sent or None


_GLOSS = re.compile(r"\s*\(([^()]*)\)\s*$")


def split_gloss(label: str) -> tuple[str, Optional[str]]:
    """'L-phenylalanine (Phe; amino acid odorant; CS in some groups)' ->
    ('L-phenylalanine (Phe)', 'amino acid odorant; CS in some groups'). A name keeps a
    trailing abbreviation — '(SST-IN)' — but an explanation is not part of a name."""
    m = _GLOSS.search(label or "")
    if not m:
        return label, None
    inner = [p.strip() for p in m.group(1).split(";") if p.strip()]
    head = label[:m.start()].rstrip()
    abbrev = [p for p in inner if len(p.split()) == 1 and len(p) <= 15]
    gloss = [p for p in inner if p not in abbrev]
    if not gloss:
        return label, None
    name = f"{head} ({'; '.join(abbrev)})" if abbrev else head
    return name, "; ".join(gloss)


def load_source_text(source_path: Optional[Path]) -> Optional[str]:
    """The text the offsets index into: the source itself when it is text, else the
    sibling <stem>.txt that input_loader writes."""
    if not source_path:
        return None
    p = Path(source_path)
    for cand in ((p,) if p.suffix.lower() in (".txt", ".md") else ()) + (p.with_suffix(".txt"),):
        if cand.is_file():
            try:
                return cand.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
    return None


def rejoin_wrapped(key: str, grp: dict) -> str:
    """A kg_plan key copied from a line-wrapped surface ('neu_rons' for "neu-\nrons")
    gets its fragments rejoined when the joined word is in the entity's reading form."""
    words = set()
    for it in grp.get("items") or []:
        words.update(fold(reading_form(str(it.get(grp.get("surf_key") or "entity") or ""))).split("_"))
    words.update(fold(grp.get("surface") or "").split("_"))
    toks, out, i = key.split("_"), [], 0
    while i < len(toks):
        if i + 1 < len(toks) and toks[i] + toks[i + 1] in words and toks[i] not in words:
            out.append(toks[i] + toks[i + 1])
            i += 2
        else:
            out.append(toks[i])
            i += 1
    return "_".join(out)


def is_non_entity(surface: str, label: Optional[str], cfg: dict) -> bool:
    """A span that names no referent (ttl_config.json non_entity_filter): a generic
    category noun, bare or led by a count/quantifier, optionally with generic
    adjectives in between. "5,000 replicable distinguishable cell types" and "these
    cells" -> True; "15 HY Gnrh1 Glut", "PV interneurons", "neurons" -> False."""
    if not cfg or (label or "") not in set(cfg.get("labels") or []):
        return False
    s = " ".join(reading_form(surface).lower().split()).strip(" .,;:()")
    heads = set(cfg.get("generic_heads") or [])
    if s in heads:
        return True
    lead = re.compile(cfg.get("count_or_quantifier") or r"$^", re.I)
    m = lead.match(s)
    if not m:
        return False
    rest = s[m.end():].split()
    adj = set(cfg.get("adjectives_before_head") or [])
    while rest and rest[0] in adj:
        rest = rest[1:]
    rest = " ".join(rest)
    return not rest or rest in heads or rest in set(cfg.get("vague_heads_after_quantifier") or [])


def drop_non_entities(result: dict, cfg: dict) -> tuple[dict, int]:
    if not cfg:
        return result, 0
    out = dict(result)
    kept = [it for it in result.get("entities") or []
            if not is_non_entity(str(it.get("entity") or ""), it.get("label"), cfg)]
    dropped = len(result.get("entities") or []) - len(kept)
    if dropped:
        out["entities"] = kept
        out.pop("entities_grouped", None)  # rebuilt from the kept raw mentions
    return out, dropped


def extraction_variant(result: dict, explicit: Optional[str] = None) -> str:
    """Which extraction of the paper this is: 'ner:neuroscience', 'ner:cns-cells', ...
    Explicit > run_metadata.variant > task_type + ner_domain (run_metadata or top level)."""
    if explicit:
        return explicit
    rm = result.get("run_metadata") or {}
    if rm.get("variant"):
        return str(rm["variant"])
    task = result.get("task_type") or "ner"
    domain = rm.get("ner_domain") or result.get("ner_domain")
    return f"{task}:{domain}" if domain else str(task)


def coordinated_slots(item: dict) -> list[Optional[str]]:
    """Per-element ontology ids of a coordinated span, in text order (None = '-'
    gap), or [] when the span names one thing. The count is `coordinated_elements`
    when the extractor gave it, else the number of ';' slots."""
    raw = str(item.get("ontology_id") or "")
    parts = [p for p in raw.split(";")] if raw.strip() else []
    slots: list[Optional[str]] = []
    for p in parts:
        cand = _QUALIFIER.sub("", p.split(",")[0]).strip()
        slots.append(cand if cand and cand != "-" else None)
    n = item.get("coordinated_elements")
    n = n if isinstance(n, int) and n >= 1 else len(slots)
    if n <= 1:
        return []
    if item.get("concept_mapping_provenance") != "tool":
        slots = []
    return (slots + [None] * n)[:n]


def concept_ref(oid: str, ontology: Optional[str], cfg: TtlConfig,
                registry: Any = None) -> Optional[tuple[str, str, str]]:
    """Return (iri, curie, acronym) for a tool-returned id, or None if unparseable.

    Prefix knowledge comes from ttl_config.json: `obo_prefixes` (OBO PURL space) and
    `curie_expansions` (other id spaces, used in both directions)."""
    oid = oid.strip()
    if registry is not None and re.match(r"^https?://", oid):
        oid = registry.canonical_iri(oid)  # BioPortal PURL of an OBO term -> the OBO IRI
    if registry is not None:
        if re.match(r"^https?://", oid):
            hit = registry.compact(oid)
            if hit:
                return oid, hit[0], hit[1]
        elif ":" in oid and registry.canonical(oid.split(":", 1)[0]):
            prefix = registry.canonical(oid.split(":", 1)[0])
            local = oid.split(":", 1)[1]
            iri = registry.expand(f"{prefix}:{local}")
            if iri:
                return iri, f"{prefix}:{local}", prefix
    m = re.match(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9]*)_(\S+)$", oid)
    if m:
        return oid, f"{m.group(1)}:{m.group(2)}", m.group(1)
    for prefix, template in cfg.curie_expansions.items():
        head, _, tail = template.partition("{id}")
        if oid.startswith(head) and oid.endswith(tail) and len(oid) > len(head) + len(tail):
            local = oid[len(head):len(oid) - len(tail) if tail else None]
            return oid, f"{prefix}:{local}", prefix
    m = re.match(r"^https?://identifiers\.org/([A-Za-z][\w.]*)[:/](\S+)$", oid)
    if m:
        return oid, f"{m.group(1)}:{m.group(2)}", m.group(1)
    if re.match(r"^https?://\S+$", oid):
        # No registered namespace holds this IRI. Never invent a prefix for it: a
        # guessed prefix is how one namespace ends up under two names. Keep the IRI,
        # with a CURIE under the mapper-reported ontology only if the registry knows it.
        acr = (registry.canonical(ontology) if registry is not None and ontology else None)
        local = re.split(r"[/#]", oid.rstrip("/#"))[-1]
        return (oid, f"{acr}:{local}", acr) if acr else (oid, f"<{oid}>", "UNREGISTERED")
    m = re.match(r"^([A-Za-z][A-Za-z0-9_.]*):(\S+)$", oid) or re.match(r"^([A-Za-z]+)_(\d+)$", oid)
    if m:
        prefix, local = m.group(1), m.group(2)
        obo = cfg.obo_prefixes.get(prefix.lower())
        if obo:
            return f"{OBO}{obo}_{local}", f"{obo}:{local}", obo
        exp = {k.lower(): (k, v) for k, v in cfg.curie_expansions.items()}.get(prefix.lower())
        if exp:
            return exp[1].replace("{id}", local), f"{exp[0]}:{local}", exp[0]
        if ":" in oid:
            return cfg.curie_fallback.format(prefix=prefix, id=local), f"{prefix}:{local}", prefix
    return None


def _ontology_hub_iri(acronym: str, cfg: TtlConfig) -> Optional[str]:
    if acronym.lower() in cfg.obo_prefixes:
        return f"{OBO}{acronym.lower()}.owl"
    return None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_declared_classes(ontology_path: Path) -> set[str]:
    return load_ontology_info(ontology_path)[0]


_VOCAB: dict[str, set[str]] = {}


def load_ontology_info(ontology_path: Path) -> tuple[set[str], Optional[str]]:
    g = Graph()
    g.parse(str(ontology_path))
    classes = {str(c)[len(str(NER)):] for c in g.subjects(RDF.type, OWL.Class) if str(c).startswith(str(NER))}
    version = next((str(v) for v in g.objects(URIRef(str(NER)), OWL.versionInfo)), None)
    _VOCAB.clear()  # controlled vocabularies: ner:<scheme>/<term> individuals
    for s in set(g.subjects()):
        loc = str(s)[len(str(NER)):] if str(s).startswith(str(NER)) else ""
        if "/" in loc:
            scheme, term = loc.split("/", 1)
            _VOCAB.setdefault(scheme, set()).add(term)
    return classes, version


def load_json(path: Optional[Path], default: Any) -> Any:
    if not path:
        return default
    return json.loads(Path(path).read_text())


def _lower_index(d: dict) -> dict:
    return {k.lower(): v for k, v in (d or {}).items()}


# ---------------------------------------------------------------------------
# Result -> entity groups
# ---------------------------------------------------------------------------

def _raw_items(result: dict) -> list[tuple[str, dict]]:
    return [(g["kind"], it) for g in mention_groups(result) for it in g["items"]]


build_groups = mention_groups


def _resource_items(result: dict) -> list[dict]:
    container = (result.get("judge_resource") or result.get("aligned_resources")
                 or result.get("extracted_resources") or {})
    out: list[dict] = []
    if isinstance(container, dict):
        for items in container.values():
            out.extend(i for i in (items or []) if isinstance(i, dict) and i.get("name"))
    elif isinstance(container, list):
        out.extend(i for i in container if isinstance(i, dict) and i.get("name"))
    return out


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

class TurtleBuilder:
    def __init__(self, result: dict, *, kg_plan: Optional[dict], label_map: dict,
                 synonyms: dict[str, str], declared: set[str], paper_slug: str,
                 kb_ns: str, profile: str, run_id: str, checked_date: str,
                 source_checksum: Optional[str], media_type: Optional[str],
                 cfg: Optional[TtlConfig] = None, ontology_version: Optional[str] = None,
                 variant: Optional[str] = None, source_text: Optional[str] = None):
        self.cfg = cfg or TtlConfig()
        self.source_text = source_text
        self.vocab = _VOCAB
        from group_by_entity import document_vocab, use_vocab, vocab_of_items
        use_vocab(document_vocab([source_text]) if source_text else
                  vocab_of_items(result.get("entities") or []))
        self.schema_version = ontology_version
        self.config_hash = config_hash()
        self.result = result
        self.plan = kg_plan or {}
        self.plan_entities = _lower_index(self.plan.get("entities") or {})
        # ids are built from the reading form ("neurons|CellType"); a plan written
        # against a raw line-wrapped surface ("neu-\nrons|CellType") still applies
        for k, v in list(self.plan_entities.items()):
            self.plan_entities.setdefault(reading_form(k).lower(), v)
        self.label_map = label_map
        self.synonyms = synonyms
        self.declared = declared
        self.slug = paper_slug
        self.profile = profile
        self.run_id = run_id
        self.date = checked_date
        self.meta = result.get("source_metadata") or {}
        self.run_meta = result.get("run_metadata") or {}
        self.generated_at = utc_now()
        self.checksum = source_checksum
        self.media_type = media_type
        self.EX = Namespace(f"{kb_ns.rstrip('/')}/{paper_slug}/")   # slug scheme only
        self.KB = Namespace(kb_ns if kb_ns.endswith("/") else kb_ns + "/")
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (result.get("source_metadata") or {}).get("doi") or "", flags=re.I)
        self.paper_id = str(self.meta.get("source_id") or doi or self.meta.get("source_path")
                            or self.meta.get("sha256") or source_checksum or paper_slug)
        # Entities and source occurrences share identity across extraction variants.
        # Annotation/classification/review records retain their separate readings.
        self.variant = extraction_variant(result, variant)
        from prefixes import PrefixRegistry
        self.registry = PrefixRegistry()
        self.g = Graph()
        for p, ns in (("ner", NER), ("kb", self.KB), ("obo", OBO), ("skos", SKOS), ("dcterms", DCTERMS),
                      ("prov", PROV), ("rdfs", RDFS), ("xsd", XSD), ("owl", OWL)):
            self.g.bind(p, ns)
        if self.cfg.iri_scheme == "slug":
            self.g.bind("ex", self.EX)
        self.warnings: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.entities_by_key: dict[str, dict] = {}
        self.plan_key_aliases: dict[str, str] = {}
        self.mention_nodes: dict[tuple, URIRef] = {}
        self.item_mentions: dict[int, URIRef] = {}
        self.classifications: dict[str, URIRef] = {}  # reading -> shared EntityClassification
        self.coordinated: list[dict] = []  # coordinated spans, resolved into components after all entities
        self.entity_by_group_id: dict[str, dict] = {}
        self.agents: dict[str, URIRef] = {}
        self.concepts: dict[str, URIRef] = {}
        self.ontology_versions: dict[str, URIRef] = {}
        self.sentences: dict[str, URIRef] = {}
        self.sections: dict[str, URIRef] = {}
        self.used_iri_slugs: set[str] = set()
        judge_block = result.get("judge_ensemble") or {}
        self.reviews = _lower_index(judge_block.get("reviews") or {})
        self.judge_models = judge_block.get("models") or {}
        self.judge_method = self._judge_method()
        labels = {(it.get("label") or "") for _, it in _raw_items(result)}
        general = set(label_map.get("general_domain_labels") or []) | {"Other", ""}
        self.general_mode = bool(labels) and labels <= general and labels != {"", "Other"}

    # ---- small helpers ----------------------------------------------------
    def add(self, s, p, o):
        self.g.add((s, p, o))

    def lit(self, v, dtype=None):
        # xsd:string literals are written plain: identical in RDF 1.1, and a plain
        # "..." is what every hand-written SPARQL query (and every store) matches.
        if dtype is None or dtype == XSD.string:
            return Literal(str(v))
        return Literal(v, datatype=dtype)

    def dec(self, v) -> Optional[Literal]:
        try:
            return Literal(Decimal(str(v)), datatype=XSD.decimal)
        except (InvalidOperation, ValueError, TypeError):
            return None

    def label(self, node, text):
        """rdfs:label is a NAME, never an explanation (ttl_config.json `labels`). A name
        over max_length keeps its full text as rdfs:comment and is cut at a word."""
        text = " ".join(str(text).split())
        cap = self.cfg.label_max_length
        if cap and len(text) > cap:
            self.add(node, RDFS.comment, Literal(text))
            cut = text[:cap - 1].rsplit(" ", 1)[0] if " " in text[:cap - 1] else text[:cap - 1]
            if len(cut) < cap // 2:  # a long unbroken token: cut inside it, not before it
                cut = text[:cap - 1]
            text = cut.rstrip(" ,;:") + "…"
        self.add(node, RDFS.label, Literal(text))

    def mint(self, kind: str, local: str) -> URIRef:
        """A paper-scoped node: UUIDv5 of `paper_name` (see ttl_config.json `iri`)."""
        if self.cfg.iri_scheme == "slug":
            return self.EX[f"{kind}-{slugify(local, max_len=60)}"]
        tpl = self.cfg.paper_shared_name if kind in self.cfg.paper_shared_kinds else self.cfg.paper_name
        name = tpl.format(kind=kind, paper=self.paper_id, variant=self.variant, local=local)
        return self.KB[str(uuid.uuid5(self.cfg.uuid_ns, name))]

    def mint_global(self, kind: str, **fields) -> URIRef:
        """A node shared across papers (entity by key, concept by IRI, ontology by acronym)."""
        template = self.cfg.global_names.get(kind)
        if self.cfg.iri_scheme == "slug" or not template:
            return self.EX[f"{kind}-{slugify('-'.join(str(v) for v in fields.values()), max_len=70)}"]
        return self.KB[str(uuid.uuid5(self.cfg.uuid_ns, template.format(**fields)))]

    def unique_slug(self, base: str) -> str:
        slug, n = base, 2
        while slug in self.used_iri_slugs:
            slug, n = f"{base}-{n}", n + 1
        self.used_iri_slugs.add(slug)
        return slug

    def _judge_method(self) -> Optional[str]:
        stats_j = ((self.result.get("stats") or {}).get("judge") or {}).get("method")
        if self.result.get("judge_ensemble"):
            return "ensemble"
        return stats_j

    # ---- spine ------------------------------------------------------------
    def build_spine(self):
        EX = self.EX
        self.pub, self.docv = self.mint("publication", "1"), self.mint("document_version", self.checksum or self.meta.get("sha256") or "1")
        self.run, self.snapshot = self.mint("run", self.run_id), self.mint("snapshot", self.run_id)
        title = self.meta.get("paper_title") or self.meta.get("title")
        doi = (self.meta.get("doi") or "").strip()
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)

        self.add(self.pub, RDF.type, NER.SourceDocument)
        if doi or self.meta.get("pmid") or self.meta.get("journal") or self.meta.get("source_type") == "publication":
            self.add(self.pub, RDF.type, NER.Publication)
        self.add(self.pub, NER.sourceIdentifier, self.lit(self.paper_id))
        self.label(self.pub, title or f"source {self.slug}")
        if title:
            self.add(self.pub, NER.title, self.lit(title, XSD.string))
        if doi:
            self.add(self.pub, NER.doi, self.lit(doi, XSD.string))
        for k in ("pmid", "pmcid"):
            if self.meta.get(k):
                self.add(self.pub, NER[k], self.lit(str(self.meta[k]), XSD.string))
        pdate = str(self.meta.get("publication_date") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pdate):
            self.add(self.pub, NER.publicationDate, Literal(pdate, datatype=XSD.date))
        year = self.meta.get("year") or (pdate[:4] if re.match(r"^\d{4}", pdate) else None)
        if year and re.match(r"^\d{4}", str(year)):
            self.add(self.pub, DCTERMS.issued, Literal(str(year)[:4], datatype=XSD.gYear))
        if self.meta.get("journal"):
            self.add(self.pub, DCTERMS.bibliographicCitation, Literal(str(self.meta["journal"])))
        self.emit_authors()
        srcs = self.meta.get("metadata_sources") or {}
        if srcs:
            biblio = ("title", "doi", "pmid", "pmcid", "year", "publication_date", "journal", "authors")
            shown = [f"{k} <- {v}" for k, v in sorted(srcs.items()) if k in biblio]
            if shown:
                self.add(self.pub, RDFS.comment, Literal("metadata read from: " + "; ".join(shown)))
        self.emit_document_versions()
        task = self.result.get("task_type") or ("resource" if _resource_items(self.result) else "ner")
        self.add(self.run, RDF.type, NER.NERExtractionActivity)
        self.label(self.run, f"structsense {task} extraction run")
        self.add(self.run, RDFS.comment, Literal(f"extraction variant: {self.variant}"))
        self.add(self.run, NER.taskType, self.lit(f"{task} + concept mapping + TTL representation", XSD.string))
        self.add(self.run, NER.runIdentifier, self.lit(self.run_id, XSD.string))
        self.add(self.run, PROV.used, self.docv)
        for prop, key in ((PROV.startedAtTime, "started_at"), (PROV.endedAtTime, "ended_at")):
            t = as_datetime(self.run_meta.get(key))
            if t is not None:
                self.add(self.run, prop, t)
        # WITH which settings: the configuration in effect, by content hash
        cfg_node = self.mint("configuration", self.config_hash)
        self.add(cfg_node, RDF.type, NER.ConfigurationArtifact)
        self.add(cfg_node, NER.configurationHash, self.lit(self.config_hash, XSD.string))
        if self.schema_version:
            self.add(cfg_node, NER.schemaVersion, self.lit(self.schema_version, XSD.string))
        self.label(cfg_node, f"structsense configuration {self.config_hash[:19]}")
        self.add(self.run, PROV.used, cfg_node)
        self.add(self.run, NER.usedConfiguration, cfg_node)
        pipeline_agent = self.agent(f"pipeline:structsense:{SKILL_VERSION}", NER.PipelineAgent,
                                    f"structsense {SKILL_VERSION}", version=SKILL_VERSION)
        self.add(self.run, PROV.wasAssociatedWith, pipeline_agent)
        sw = self.mint_global("agent", key=f"software:structsense:{SKILL_VERSION}")
        self.add(sw, RDF.type, NER.SoftwareArtifact)
        self.label(sw, f"structsense skill {SKILL_VERSION}")
        self.add(sw, NER.softwareVersion, self.lit(SKILL_VERSION, XSD.string))
        self.add(self.run, NER.usedSoftware, sw)

        self.add(self.snapshot, RDF.type, NER.ExtractionSnapshot)
        self.add(self.snapshot, NER.snapshotOfDocumentVersion, self.docv)
        self.add(self.snapshot, PROV.wasGeneratedBy, self.run)
        self.add(self.snapshot, PROV.generatedAtTime, as_datetime(self.generated_at))
        self.label(self.snapshot, f"extraction snapshot {self.run_id}")

        align = (self.result.get("stats") or {}).get("alignment") or {}
        self.mapper_name = align.get("mapper_used") or "concept-mapping tool"
        self.map_run = self.mint("run", f"{self.run_id}|mapping")
        self.add(self.map_run, RDF.type, NER.ConceptMappingActivity)
        self.label(self.map_run, f"concept mapping via {self.mapper_name}")
        self.add(self.map_run, PROV.wasInformedBy, self.run)
        self.add(self.map_run, NER.runIdentifier, self.lit(f"{self.run_id}-mapping", XSD.string))
        self.add(self.map_run, NER.taskType, self.lit("concept mapping", XSD.string))
        for prop, key in ((PROV.startedAtTime, "started_at"), (PROV.endedAtTime, "ended_at")):
            t = as_datetime(align.get(key))
            if t is not None:
                self.add(self.map_run, prop, t)
        self.align_meta = align
        self.source_runs: dict[str, URIRef] = {}
        if align.get("config_hash"):
            mcfg = self.mint("configuration", align["config_hash"])
            self.add(mcfg, RDF.type, NER.ConfigurationArtifact)
            self.add(mcfg, NER.configurationHash, self.lit(align["config_hash"], XSD.string))
            self.label(mcfg, f"concept-mapping configuration {align['config_hash'][:19]}")
            self.add(self.map_run, PROV.used, mcfg)
            self.add(self.map_run, NER.usedConfiguration, mcfg)
        for t_ont in align.get("trusted_ontologies") or []:
            self.add(self.map_run, RDFS.comment, Literal(
                f"trusted ontology {t_ont.get('name')} ({t_ont.get('file')}, priority "
                f"{t_ont.get('priority')}, version {t_ont.get('version')})"))

        self.judge_run = self.mint("run", f"{self.run_id}|judge")
        self.add(self.judge_run, RDF.type, NER.AutomaticValidationActivity)
        self.add(self.judge_run, PROV.wasInformedBy, self.run)
        self.add(self.judge_run, NER.runIdentifier, self.lit(f"{self.run_id}-judge", XSD.string))
        self.add(self.judge_run, NER.taskType, self.lit(f"judge ({self.judge_method or 'none'})", XSD.string))
        self.label(self.judge_run, f"judge stage ({self.judge_method or 'not run'})")
        t = as_datetime((self.result.get("judge_ensemble") or {}).get("combined_at"))
        if t is not None:
            self.add(self.judge_run, PROV.endedAtTime, t)
        if self.judge_method in (None, "auto_approved"):
            self.add(self.judge_run, RDFS.comment, Literal(
                "No quality judging was performed (auto-approved); items carry no review decisions."))

    def agent(self, key: str, cls, label: str, version: Optional[str] = None) -> URIRef:
        if key in self.agents:
            return self.agents[key]
        node = self.mint_global("agent", key=key)
        self.add(node, RDF.type, cls)
        self.label(node, label)
        if version:  # WHICH version ran (ner:agentVersion, on NamedEntitySoftwareAgent and subclasses)
            self.add(node, NER.agentVersion, self.lit(str(version), XSD.string))
        self.agents[key] = node
        return node

    def emit_authors(self):
        """Who wrote the paper, as PROV: the publication prov:wasAttributedTo (and
        dcterms:creator) each author, a prov:Person shared across papers (keyed by ORCID
        when known, else by name). Author order is the publication's own statement."""
        authors = []
        for a in self.meta.get("authors") or []:
            a = {"name": a} if isinstance(a, str) else a
            if isinstance(a, dict) and (a.get("name") or "").strip():
                authors.append(a)
        for a in authors:
            name = " ".join(str(a["name"]).split())
            orcid = re.sub(r"^https?://orcid\.org/", "", str(a.get("orcid") or "")).strip()
            key = f"orcid:{orcid}" if orcid else "person:" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            node = self.mint_global("agent", key=key)
            self.add(node, RDF.type, PROV.Person)
            self.label(node, name)
            if orcid:
                self.add(node, DCTERMS.identifier, Literal(f"https://orcid.org/{orcid}"))
            self.add(self.pub, PROV.wasAttributedTo, node)
            self.add(self.pub, DCTERMS.creator, node)
        if authors:
            names = "; ".join(" ".join(str(a["name"]).split()) for a in authors)
            partial = " (partial: first author only, from PDF metadata)" if self.meta.get("authors_partial") else ""
            self.add(self.pub, RDFS.comment, Literal(f"authors, in order{partial}: {names}"))

    def emit_document_versions(self):
        """The document as received (PDF / XML / DOCX ...) and, when the text was
        extracted from it, the text actually processed: a second DocumentVersion,
        prov:wasDerivedFrom the first and generated by a ner:DocumentIngestionActivity
        naming the backend. Mentions, sentences and offsets belong to the text."""
        sp = self.meta.get("source_path")
        tp = self.meta.get("text_path")
        ing = self.meta.get("ingestion") or {}
        src_is_text = not sp or (tp and Path(str(sp)).resolve() == Path(str(tp)).resolve()) or \
            (not tp and Path(str(sp)).suffix.lower() in (".txt", ".md"))
        media = lambda p: self.cfg.media_types.get(Path(str(p)).suffix.lower()) if p else None  # noqa: E731

        def version(node, path, checksum, mtype):
            self.add(node, RDF.type, NER.DocumentVersion)
            self.add(node, NER.versionOfDocument, self.pub)
            self.add(self.pub, NER.hasDocumentVersion, node)
            if mtype:
                self.add(node, NER.mediaType, self.lit(mtype, XSD.string))
            if checksum:
                self.add(node, NER.checksum, self.lit(checksum, XSD.string))
            if path:  # the file NAME only by default: a local path is private and not portable
                shown = str(path) if self.cfg.source_path_mode == "full" else Path(str(path)).name
                self.add(node, NER.sourcePath, self.lit(shown, XSD.string))

        if src_is_text:
            version(self.docv, sp or tp, self.checksum or ing.get("text_sha256"), self.media_type or media(sp or tp))
            self.label(self.docv, "document version")
            return
        orig = self.mint("document_version", "source")
        version(orig, sp, ing.get("source_sha256") or _sha256_if_file(sp), media(sp))
        self.label(orig, "source document")
        if ing.get("page_count"):
            self.add(orig, RDFS.comment, Literal(f"{ing['page_count']} pages"))
        version(self.docv, tp, ing.get("text_sha256") or self.checksum, media(tp) or "text/plain")
        self.label(self.docv, "extracted text")
        self.add(self.docv, PROV.wasDerivedFrom, orig)
        act = self.mint("document_ingestion", "1")
        self.add(act, RDF.type, NER.DocumentIngestionActivity)
        self.label(act, "text extraction")
        self.add(act, PROV.used, orig)
        self.add(self.docv, PROV.wasGeneratedBy, act)
        t = as_datetime(ing.get("at"))
        if t is not None:
            self.add(act, PROV.endedAtTime, t)
        backend = ing.get("backend")
        if backend:
            self.add(act, PROV.wasAssociatedWith, self.agent(
                f"text-extractor:{backend}", NER.NamedEntitySoftwareAgent, backend,
                version=ing.get("backend_version") or backend))
        parts = [f"backend {backend}" if backend else None,
                 f"{ing['chars']} characters" if ing.get("chars") else None,
                 f"docling {'on' if ing.get('docling') else 'off'}" if "docling" in ing else None,
                 f"grobid {'on' if ing.get('grobid') else 'off'}" if "grobid" in ing else None]
        if any(parts):
            self.add(act, RDFS.comment, Literal("; ".join(p for p in parts if p)))

    def source_agent(self, source_model: Optional[str]) -> URIRef:
        sm = source_model or "llm_ner:unknown"
        if sm.startswith("llm_ner:") or sm.startswith("llm:"):
            model = sm.split(":", 1)[1] or "unknown LLM"
            node = self.agent(sm, NER.LanguageModelAgent, model, version=model)
        else:
            node = self.agent(sm, NER.NamedEntitySoftwareAgent, sm, version=sm)
        self.add(self.run, PROV.wasAssociatedWith, node)
        return node

    # ---- classes ------------------------------------------------------------
    def resolve_class(self, label: Optional[str], kind: str, plan: dict) -> tuple[str, Optional[str]]:
        override = plan.get("class")
        if override:
            if override in self.declared:
                return override, None
            self.warnings.append(f"kg_plan class {override!r} is not declared in the ontology; ignored")
        if kind == "key_term" and not label:
            return self.label_map.get("key_term_class", "ScientificNamedEntity"), None
        # the ontology's own class of that name first, so new classes are used as soon
        # as the ontology declares them; the map covers labels without one
        if label and label in self.declared:
            return label, None
        mapped = (self.label_map.get("labels") or {}).get(label or "")
        if mapped and mapped in self.declared:
            return mapped, None
        fb = (self.label_map.get("general_fallback_class") if self.general_mode
              else self.label_map.get("fallback_class")) or "NamedEntity"
        self.counts["labels_fell_back"] += 1
        return fb, f"extractor label {label!r} has no class in the ontology; typed as ner:{fb}"

    # ---- entities -----------------------------------------------------------
    def plan_for(self, gid: str) -> dict:
        return self.plan_entities.get(gid.lower()) or {}

    IDENTITY_MATCH_TYPES = ("label", "exact_synonym", "symbol")

    def trusted(self):
        """The trusted-ontology lexicon (concept_mapping.py), or None if not indexed."""
        if not hasattr(self, "_trusted"):
            self._trusted = None
            try:
                from concept_mapping import TrustedMapper, load_config
                tm = TrustedMapper(load_config())
                if tm.lexicon.db_path.is_file():
                    self._trusted = tm
            except Exception as e:  # keys still derive algorithmically
                self.warnings.append(f"trusted lexicon unavailable for key derivation: {e}")
        return self._trusted

    def derive_key(self, grp: dict) -> str:
        """normalizedEntityKey without a kg_plan entry (references/key-normalization.md):
        Resolve explicit identities and local homonyms first, then source-defined
        aliases and context-reviewed exact mappings. Fall back to surface
        normalization. A fresh lexical hit must not override contextual review."""
        identities = {it.get("identity_key") for it in grp["items"] if it.get("identity_key")}
        if len(identities) == 1:
            return normalize_key(str(next(iter(identities))), {})
        if grp.get("referent_id"):
            # A local disambiguator is not a cross-source identity assertion.
            scope = hashlib.sha256(self.paper_id.encode()).hexdigest()[:12]
            return normalize_key(f"{grp['referent_id']}_{scope}", {})
        user = self.synonyms.get(fold(grp["surface"]))
        if user:
            return user
        # Source-defined abbreviation aliases are evidence; a fresh dictionary
        # lookup is not. Never undo a judge's rejected/broad mapping to merge keys.
        if not hasattr(self, "_abbreviations"):
            from concept_mapping import abbreviation_table
            self._abbreviations = abbreviation_table(
                [self.source_text] if self.source_text else
                [it.get("sentence") for _, it in _raw_items(self.result)])
        if grp["surface"] in self._abbreviations:
            return normalize_key(self._abbreviations[grp["surface"]], self.synonyms)
        for it in grp["items"]:
            if (it.get("concept_mapping_provenance") == "tool" and it.get("ontology_label")
                    and it.get("mapping_tier") == "exactMatch"):
                self.counts["keys_from_mapping_label"] += 1
                return normalize_key(it["ontology_label"], self.synonyms)
        return normalize_key(grp["surface"], self.synonyms)

    def build_entities(self, groups: list[dict]):
        for grp in groups:
            plan = self.plan_for(grp["id"])
            cls, class_note = self.resolve_class(grp["label"], grp["kind"], plan)
            from_plan = bool(plan.get("normalized_key")) or len({it.get("identity_key") for it in grp["items"] if it.get("identity_key")}) == 1
            key = plan.get("normalized_key") or self.derive_key(grp)
            if from_plan:
                key = rejoin_wrapped(key, grp)
            if not key:
                key = f"{fold(self.slug)}_{slugify(grp['surface']).replace('-', '_')}"
            general_labels = set(self.label_map.get("general_domain_labels") or [])
            reviewed_exact = any(it.get("concept_mapping_provenance") == "tool" and
                                 it.get("mapping_tier") == "exactMatch" for it in grp["items"])
            if not from_plan and not reviewed_exact and grp["label"] in general_labels:
                # A bare name in a note is not proof of global identity. A reviewed
                # plan/identity_key later removes this source-local qualification.
                scope = hashlib.sha256(self.paper_id.encode()).hexdigest()[:12]
                key = f"{key}_{class_snake(cls)}_{scope}"
            if key in self.cfg.generic_keys:
                # A generic referent is paper-local until someone says otherwise; a
                # missed merge is recoverable, a wrong merge is not.
                new = f"{fold(self.slug)}_{key}"
                self.warnings.append(f"generic key {key!r} for {grp['id']!r} qualified as {new!r} "
                                     f"(set normalized_key in kg_plan to fix)")
                self.plan_key_aliases[key] = new
                key = new
            ent = self.entities_by_key.get(key)
            if ent and not from_plan and cls not in ent["classes"] and not ent["from_plan"]:
                new = f"{key}_{class_snake(cls)}"
                self.warnings.append(f"key {key!r} already used by ner:{sorted(ent['classes'])[0]}; "
                                     f"{grp['id']!r} (ner:{cls}) keyed {new!r}")
                key, ent = new, self.entities_by_key.get(new)
            if ent is None:
                slug = self.unique_slug("entity-" + key.replace("_", "-")[:70])
                ent = {"key": key, "node": self.mint_global("entity", key=key), "slug": slug, "classes": set(),
                       "groups": [], "from_plan": from_plan, "notes": [],
                       "label": plan.get("normalized_label") or grp["surface"], "plans": []}
                self.entities_by_key[key] = ent
            ent["classes"].add(cls)
            ent["groups"].append(grp)
            ent["plans"].append(plan)
            ent["from_plan"] = ent["from_plan"] or from_plan
            if class_note:
                ent["notes"].append(class_note)
            self.entity_by_group_id[grp["id"].lower()] = ent

        for ent in self.entities_by_key.values():
            self.emit_entity(ent)
        self.emit_components()

    def emit_entity(self, ent: dict):
        node = ent["node"]
        for cls in sorted(ent["classes"]):
            self.add(node, RDF.type, NER[cls])
        self.add(node, RDF.type, NER.NamedEntity)
        self.add(node, NER.normalizedEntityKey, self.lit(ent["key"], XSD.string))
        self.entity_name(node, ent)
        self.add(node, PROV.hadPrimarySource, self.pub)
        self.add(node, PROV.wasGeneratedBy, self.run)
        self.add(self.snapshot, PROV.hadMember, node)
        for note in dict.fromkeys(ent["notes"]):
            self.add(node, RDFS.comment, Literal(note))
        ent["decisions"] = {}
        self.emit_mapping(ent)  # first: mentions' annotation versions point at its decisions
        self.refine_class(ent)
        mention_n = 0
        for grp in ent["groups"]:
            for it in grp["items"]:
                mention_n += 1
                self.emit_mention(ent, grp, it, mention_n)
        self.counts["entities"] += 1
        if self.profile == "full":
            self.emit_reviews(node, ent["slug"], [g["id"] for g in ent["groups"]], ent)

    def emit_mention(self, ent: dict, grp: dict, it: dict, n: int):
        EX = self.EX
        surface = it.get(grp["surf_key"]) or grp["surface"]
        a, b = it.get("start"), it.get("end")
        occurrence = (ent["key"], a, b) if isinstance(a, int) and isinstance(b, int) else (ent["key"], "unanchored", n)
        m = self.mention_nodes.get(occurrence)
        if m is not None:
            self.item_mentions[id(it)] = m
            return
        m = self.mint("mention", str(self.docv) + "|" + "|".join(map(str, occurrence)))
        self.mention_nodes[occurrence] = m
        self.item_mentions[id(it)] = m
        self.add(ent["node"], NER.hasMention, m)
        self.add(m, NER.refersToEntity, ent["node"])
        self.add(m, RDF.type, NER.EntityMention)
        self.add(m, NER.surfaceForm, self.lit(surface, XSD.string))
        self.label(m, reading_form(surface))  # surfaceForm above stays byte-exact for the offsets
        self.add(m, NER.partOfDocumentVersion, self.docv)
        start, end = it.get("start"), it.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end:
            self.add(m, NER.documentStartOffset, self.lit(start, XSD.nonNegativeInteger))
            self.add(m, NER.documentEndOffset, self.lit(end, XSD.nonNegativeInteger))
        self.counts["mentions"] += 1
        agent = self.source_agent(it.get("source_model"))
        slots = coordinated_slots(it)
        if slots:
            self.add(m, RDF.type, NER.CoordinatedEntityMention)
            self.add(m, NER.coordinatedElementCount, self.lit(len(slots), XSD.positiveInteger))
            self.coordinated.append({"ent": ent, "mention": m, "n": n, "surface": surface,
                                     "item": it, "slots": slots, "agent": agent})
        sent = self.mention_sentence(it, surface)
        if sent:
            self.add(m, NER.inSentence, self.sentence_node(sent))
        loc = it.get("paper_location")
        if loc:
            self.add(m, NER.inSection, self.section_node(str(loc)))
        if self.profile != "full":
            return
        av = self.mint("annotation_version", f"{ent['key']}|{a}|{b}|{grp['id']}|1")
        self.add(av, RDF.type, NER.EntityAnnotationVersion)
        self.label(av, "annotation v1")
        self.add(av, NER.annotationOfMention, m)
        self.add(av, NER.refersToNormalizedEntity, ent["node"])
        self.add(av, NER.revisionNumber, self.lit(1, XSD.positiveInteger))
        self.add(av, NER.inSnapshot, self.snapshot)
        self.add(av, PROV.wasAttributedTo, agent)
        self.add(m, NER.hasAnnotationVersion, av)
        self.add(m, NER.hasCurrentAnnotationVersion, av)
        ent.setdefault("avs", {}).setdefault(grp["id"], []).append(av)
        # One classification node per distinct READING of the entity (class, raw label,
        # confidence, specificity), shared by every mention read that way — not one node
        # per mention repeating the entity's own rdf:type hundreds of times.
        cls = sorted(ent["classes"])[0]
        raw = grp["label"] or "KeyTerm"
        # the judged confidence of this reading when the ensemble ran, else the
        # surfacing model's own score (HF NER models emit one)
        score = it.get("judge_score") if it.get("judge_method") == "ensemble" else it.get("source_score")
        score = round(float(score), 4) if isinstance(score, (int, float)) and 0 <= score <= 1 else None
        spec = it.get("specificity")
        spec = spec if spec in ("cell_phenotype", "cell_vague", "cell_hetero", "unspecified") else None
        reading = f"{ent['key']}|{cls}|{raw}|{score}|{spec}"
        cl = self.classifications.get(reading)
        if cl is None:
            cl = self.mint("classification", reading)
            self.classifications[reading] = cl
            self.add(cl, RDF.type, NER.EntityClassification)
            self.label(cl, f"{ent['label']} — {raw} classification" + (f" ({spec})" if spec else ""))  # rdf:type already says "classification"
            self.add(cl, NER.classifiedAsClass, NER[cls])
            self.add(cl, NER.classificationLabelRaw, self.lit(raw, XSD.string))
            if score is not None:
                self.add(cl, NER.classificationConfidence, self.dec(score))
            if spec:
                self.add(cl, NER.hasSpecificityCategory, NER[f"specificity/{spec}"])
                self.add(cl, NER.specificityLabelRaw, self.lit(spec, XSD.string))
        if it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id"):
            for single, _hint in split_ontology_ids(str(it["ontology_id"])):
                pair = ent.get("decisions", {}).get(single)
                if pair:
                    self.add(av, NER.hasMappingDecision, pair[0])
                    self.add(av, NER.hasMappingCandidate, pair[1])
        self.add(av, NER.hasClassification, cl)

    def emit_components(self):
        """One component EntityMention per element of a coordinated span ("SST and
        PV interneurons" -> 2), in text order (cns-cells `coordinated_elements`;
        references/cell-annotation-conventions.md). A component belongs to the
        paper's own entity for that element's concept when one exists (so the span
        counts as a mention of it), else to the span's entity; its annotation
        version carries only that element's mapping decision. A '-' slot is kept as
        an honest gap. The ontology has no ordinal property yet, so the position is
        in the label and comment (ttl-representation.md, ontology fixes)."""
        by_concept: dict[str, dict] = {}
        for ent in self.entities_by_key.values():
            ids = ent.get("concept_ids") or []
            if len(ids) == 1 and not ent.get("coordinated"):
                by_concept.setdefault(ids[0], ent)
        for rec in self.coordinated:
            span_ent, m, total = rec["ent"], rec["mention"], len(rec["slots"])
            for i, oid in enumerate(rec["slots"], 1):
                owner = by_concept.get(oid) if oid else None
                owner = owner or span_ent
                c = self.mint("mention", f"{span_ent['key']}|{rec['n']}|element|{i}")
                self.add(owner["node"], NER.hasMention, c)
                self.add(c, RDF.type, NER.EntityMention)
                self.add(c, NER.surfaceForm, self.lit(rec["surface"], XSD.string))
                self.add(c, NER.partOfDocumentVersion, self.docv)
                self.add(c, NER.componentOfMention, m)
                self.add(m, NER.hasComponentMention, c)
                self.label(c, f"element {i}/{total}")
                self.add(c, RDFS.comment, Literal(
                    f"Element {i} of {total} of a coordinated span; "
                    + (f"ontology term {oid}." if oid else "no ontology term for this element (gap).")))
                self.counts["component_mentions"] = self.counts.get("component_mentions", 0) + 1
                if self.profile != "full":
                    continue
                sent = self.mention_sentence(rec["item"], rec["surface"])
                if sent:
                    self.add(c, NER.inSentence, self.sentence_node(sent))
                av = self.mint("annotation_version", f"{span_ent['key']}|{rec['n']}|element|{i}|1")
                self.add(av, RDF.type, NER.EntityAnnotationVersion)
                self.label(av, "annotation v1")
                self.add(av, NER.annotationOfMention, c)
                self.add(av, NER.refersToNormalizedEntity, owner["node"])
                self.add(av, NER.revisionNumber, self.lit(1, XSD.positiveInteger))
                self.add(av, NER.inSnapshot, self.snapshot)
                self.add(av, PROV.wasAttributedTo, rec["agent"])
                self.add(c, NER.hasAnnotationVersion, av)
                self.add(c, NER.hasCurrentAnnotationVersion, av)
                pair = (owner.get("decisions") or {}).get(oid) or (span_ent.get("decisions") or {}).get(oid) if oid else None
                if pair:
                    self.add(av, NER.hasMappingDecision, pair[0])
                    self.add(av, NER.hasMappingCandidate, pair[1])

    def entity_name(self, node, ent: dict):
        """normalizedEntityLabel + rdfs:label are the NAME; a gloss the planner put in
        parentheses, and the plan's `note`, become rdfs:comment strings."""
        name, gloss = split_gloss(ent["label"])
        self.add(node, NER.normalizedEntityLabel, self.lit(name, XSD.string))
        self.label(node, name)
        if gloss:
            self.add(node, RDFS.comment, Literal(gloss))
        for plan in ent.get("plans") or []:
            note = plan.get("note") if isinstance(plan, dict) else None
            if isinstance(note, str) and note.strip():
                self.add(node, RDFS.comment, Literal(note.strip()))

    def mention_sentence(self, it: dict, surface: str) -> Optional[str]:
        """The item's sentence when it contains the surface; else, when the source text
        is at hand and the offsets select the surface, the sentence around the offsets
        (so a sentence cut at a line-wrap hyphen is repaired, not propagated)."""
        # with the source text at hand the sentence is ALWAYS derived from it, so every
        # extraction variant that finds this span links the same Sentence node (the
        # mention IRI is shared across variants); the item's own sentence is a fallback
        sent = it.get("sentence")
        start, end, text = it.get("start"), it.get("end"), self.source_text
        if text and isinstance(start, int) and isinstance(end, int) and text[start:end] == surface:
            canon = sentence_around(text, start, end)
            if canon and surface in canon:
                return canon
        if sent and surface in sent:
            return sent
        if text and isinstance(start, int) and isinstance(end, int) and text[start:end] == surface:
            fixed = sentence_around(text, start, end)
            if fixed and surface in fixed:
                if sent:
                    self.counts["sentences_repaired"] += 1
                return fixed
        return sent

    def sentence_node(self, text: str) -> URIRef:
        if text not in self.sentences:
            node = self.mint("sentence", str(self.docv) + "|" + hashlib.sha1(text.encode()).hexdigest())
            self.add(node, RDF.type, NER.Sentence)
            self.add(node, NER.sentenceText, self.lit(text, XSD.string))
            self.add(node, NER.partOfDocumentVersion, self.docv)
            self.label(node, f"sentence {len(self.sentences) + 1}")  # the text is ner:sentenceText
            self.sentences[text] = node
        return self.sentences[text]

    def section_node(self, label: str) -> URIRef:
        if label not in self.sections:
            node = self.mint("section", str(self.docv) + "|" + label)
            self.add(node, RDF.type, NER.Section)
            self.add(node, NER.sectionLabel, self.lit(label, XSD.string))
            self.add(node, NER.partOfDocumentVersion, self.docv)
            self.label(node, f"section: {label}")
            self.sections[label] = node
        return self.sections[label]

    # ---- mappings -----------------------------------------------------------
    def emit_mapping(self, ent: dict):
        tool_ids: dict[str, dict] = {}
        gap_reasons: set[str] = set()
        for grp in ent["groups"]:
            for it in grp["items"]:
                prov = it.get("concept_mapping_provenance")
                if prov == "tool" and it.get("ontology_id"):
                    tool_ids.setdefault(str(it["ontology_id"]), it)
                elif prov and prov != "tool":
                    gap_reasons.add(it.get("alignment_method") or prov)
        plan_tier = next((p.get("skos_tier") for p in ent["plans"] if p.get("skos_tier")), None)
        plan_note = next((p.get("tier_note") for p in ent["plans"] if p.get("tier_note")), None)
        emitted = 0
        for oid, it in tool_ids.items():
            for single, hint in split_ontology_ids(oid):
                ref = concept_ref(single, it.get("ontology"), self.cfg, self.registry)
                if not ref:
                    self.warnings.append(f"unparseable ontology_id {single!r} on {ent['key']!r}; no concept written")
                    continue
                if ref[2] == "UNREGISTERED":
                    ns = single.rsplit("#", 1)[0] + "#" if "#" in single else single.rsplit("/", 1)[0] + "/"
                    self.warnings.append(f"{single} is in no registered namespace ({ns}); not written — "
                                         f"register it in ttl_config.json curie_expansions to keep prefixes consistent")
                    gap_reasons.add(f"namespace {ns} not registered")
                    continue
                # Precedence: kg_plan > mapping judge > how the tool matched > id qualifier > default.
                tier = _TIER_ALIASES.get(str(plan_tier).lower(), plan_tier) if plan_tier else None
                tier = tier or it.get("mapping_tier") or it.get("match_tier") or hint
                untiered = tier is None
                tier = tier if tier in MATCH_TIERS else self.cfg.default_tier
                concept = self.concept_node(ref, it)
                pred, rel = MATCH_TIERS[tier], self.cfg.relation_by_tier.get(tier, "close")
                self.add(ent["node"], NER.resolvedToConcept, concept)
                self.add(ent["node"], pred, URIRef(ref[0]))
                if untiered:
                    self.add(ent["node"], RDFS.comment, Literal(
                        f"Match tier for {ref[1]} was not judged; {self.cfg.default_tier} is the configured default."))
                if plan_note:
                    self.add(ent["node"], RDFS.comment, Literal(plan_note))
                # the mapping decision (candidate, rank, score, method, status,
                # confidence) is provenance of the IRI itself, so every profile keeps
                # it; compact drops only per-mention annotation/review history
                self.emit_decision(ent, concept, ref, rel, it, single)
                ent.setdefault("concept_ids", []).append(single)
                ent.setdefault("mapped_tiers", []).append((ref[0], tier))
                ent["coordinated"] = ent.get("coordinated") or bool(coordinated_slots(it))
                emitted += 1
                self.counts["mapped_concepts"] += 1
        if not emitted:
            why = ", ".join(sorted(gap_reasons)) or "no mapping returned"
            self.add(ent["node"], RDFS.comment, Literal(
                f"No tool-verified ontology mapping ({why}); checked via {self.mapper_name} on {self.date}."))
            self.counts["unmapped_entities"] += 1
            self.brainkb_default_concept(ent)

    def refine_class(self, ent: dict) -> None:
        """A generic cell class ('CellType') mapped exact/close/broad to a CL term below
        an anchor (CL:0000540 neuron, CL:0000129 microglial cell, ...) also gets the
        anchor's NER class (ttl_config.json class_from_concept; scripts/class_anchors)."""
        cfg = self.cfg.raw.get("class_from_concept") or {}
        if not cfg.get("anchors") or not (set(ent["classes"]) & set(cfg.get("applies_to") or [])):
            return
        from class_anchors import lookup
        tiers = set(cfg.get("tiers") or ["exactMatch", "closeMatch", "broadMatch"])
        added = set()
        for iri, tier in ent.get("mapped_tiers") or []:
            if tier in tiers:
                for cls in lookup(iri):
                    if cls in self.declared and cls not in ent["classes"]:
                        added.add(cls)
        for cls in sorted(added):
            self.add(ent["node"], RDF.type, NER[cls])
        if added:
            self.counts["classes_refined"] += 1

    def brainkb_default_concept(self, ent: dict) -> None:
        """No source mapped this entity: link it to a provisional BrainKB concept
        (ttl_config.json unmapped_default), deterministic by key so the same referent
        gets the same concept in every paper. Not an external mapping: no mapping
        decision is written, and the concept says it is a BrainKB default."""
        cfg = (self.cfg.raw.get("unmapped_default") or {})
        if not cfg.get("enabled"):
            return
        base, prefix = cfg.get("base", "https://brainkb.org/concept/"), cfg.get("prefix", "BRAINKB")
        local = str(uuid.uuid5(self.cfg.uuid_ns, f"brainkb-concept|{ent['key']}"))
        iri = base + local
        # its label comes from the KEY, not this paper's wording, so every paper that
        # meets the same referent writes the same global concept
        concept = self.concept_node((iri, f"{prefix}:{local}", prefix),
                                    {"ontology_label": ent["key"].replace("_", " ")})
        if (concept, RDFS.comment, None) not in self.g:
            self.add(concept, RDFS.comment, Literal(
                "BrainKB default concept: no external ontology term was found for this entity; "
                "provisional, to be materialized or replaced by curation."))
        tier = cfg.get("tier", "exactMatch")
        self.add(ent["node"], NER.resolvedToConcept, concept)
        self.add(ent["node"], MATCH_TIERS.get(tier, SKOS.exactMatch), URIRef(iri))
        self.counts["brainkb_default_concepts"] += 1

    def concept_node(self, ref: tuple[str, str, str], it: dict) -> URIRef:
        iri, curie, acr = ref
        if curie in self.concepts:
            return self.concepts[curie]
        node = self.mint_global("concept", iri=iri)
        self.add(node, RDF.type, NER.OntologyConcept)
        self.add(node, NER.conceptIRI, self.lit(iri, XSD.anyURI))
        self.add(node, NER.conceptIdentifier, self.lit(curie, XSD.string))
        pref = it.get("ontology_label")
        if pref:
            self.add(node, NER.preferredLabel, self.lit(pref, XSD.string))
        self.label(node, pref or curie)
        self.add(node, NER.conceptInOntologyVersion, self.ontology_version(acr))
        self.concepts[curie] = node
        return node

    def ontology_version(self, acr: str) -> URIRef:
        key = acr.upper()
        if key in self.ontology_versions:
            return self.ontology_versions[key]
        s = slugify(acr)
        hub, ver = self.mint_global("ontology", acronym=acr), self.mint("ontology_version", acr)
        self.add(hub, RDF.type, NER.ExternalOntology)
        self.add(hub, NER.ontologyAcronym, self.lit(acr, XSD.string))
        self.label(hub, acr)
        hub_iri = _ontology_hub_iri(acr, self.cfg)
        if hub_iri:
            self.add(hub, NER.ontologyIRI, self.lit(hub_iri, XSD.anyURI))
        self.add(ver, RDF.type, NER.OntologyVersion)
        self.add(ver, NER.versionOfOntology, hub)
        self.add(hub, NER.hasOntologyVersion, ver)
        default = (self.cfg.raw.get("unmapped_default") or {}).get("prefix", "BRAINKB")
        vs = (f"BrainKB default concepts (structsense {SKILL_VERSION}), {self.date}" if key == default.upper()
              else f"as served by {self.mapper_name}, {self.date}")
        self.add(ver, NER.ontologyVersionString, self.lit(vs, XSD.string))
        self.label(ver, f"{acr} ({vs})")
        self.add(self.map_run, NER.usedOntologyVersion, ver)
        self.ontology_versions[key] = ver
        return ver

    def source_run(self, source: Optional[str]) -> URIRef:
        """One ConceptMappingActivity per mapping source (trusted / local_hybrid /
        bioportal / ...), each with its own method, under the overall mapping step."""
        key = re.split(r"[\s:(]", str(source or self.mapper_name), maxsplit=1)[0].lower() or "mapper"
        if key not in self.source_runs:
            node = self.mint("run", f"{self.run_id}|mapping|{key}")
            self.add(node, RDF.type, NER.ConceptMappingActivity)
            self.label(node, f"concept mapping: {source or self.mapper_name}")
            self.add(node, NER.runIdentifier, self.lit(f"{self.run_id}-mapping-{key}", XSD.string))
            self.add(node, NER.taskType, self.lit(f"concept mapping ({key})", XSD.string))
            self.add(node, PROV.wasInformedBy, self.map_run)
            method = self.cfg.method_by_source.get(key)
            if method:
                self.add(node, NER.usedMappingMethod, NER[f"mapping-method/{method}"])
            version = None
            if key == "trusted":
                version = f"trusted lexicon: {len(self.align_meta.get('trusted_ontologies') or [])} ontologies"
            agent = self.agent(f"mapper:{key}", NER.ConceptMapperAgent, f"concept mapper: {key}", version=version)
            if key == "local_hybrid" and self.align_meta.get("mapper_url"):
                self.add(agent, RDFS.seeAlso, URIRef(self.align_meta["mapper_url"]))
            self.add(node, PROV.wasAssociatedWith, agent)
            self.add(self.map_run, PROV.wasAssociatedWith, agent)
            self.source_runs[key] = node
        return self.source_runs[key]

    def emit_decision(self, ent: dict, concept: URIRef, ref, rel: str, it: dict, single: str = ""):
        base = f"{ent['key']}|{ref[1]}"
        dec, cand = self.mint("mapping_decision", base), self.mint("mapping_candidate", base)
        ent.setdefault("decisions", {})[single or ref[0]] = (dec, cand)
        self.add(cand, RDF.type, NER.ConceptMappingCandidate)
        self.add(cand, NER.candidateConcept, concept)
        self.add(cand, NER.candidateForNormalizedEntity, ent["node"])
        self.add(cand, NER.mappingRank, self.lit(1, XSD.positiveInteger))  # the selected (top) candidate
        self.label(cand, ref[1])
        score = it.get("mapping_score") or it.get("score")
        if isinstance(score, (int, float)):
            sc = self.mint("mapping_score", base)
            self.add(sc, RDF.type, NER.MappingScore)
            self.add(sc, NER.scoreType, self.lit(str(self.mapper_name), XSD.string))
            self.add(sc, NER.scoreValue, self.dec(score))
            self.label(sc, "mapping score")
            self.add(cand, NER.hasMappingScore, sc)
        self.add(dec, RDF.type, NER.ConceptMappingDecision)
        self.add(dec, NER.decisionForNormalizedEntity, ent["node"])
        self.add(dec, NER.selectedCandidate, cand)
        self.add(dec, NER.mappingRelationType, NER[f"mapping-relation/{rel}"])
        self.add(dec, NER.mappingStatus, NER["mapping-status/accepted"])
        self.add(dec, NER.conceptMappingProvenanceRaw, self.lit("tool", XSD.string))
        if it.get("alignment_method"):
            self.add(dec, NER.alignmentMethodRaw, self.lit(str(it["alignment_method"]), XSD.string))
        src = it.get("mapping_source")
        if src or it.get("ontology_match_type"):
            self.add(dec, NER.decisionReason, self.lit(
                f"matched by {src or self.mapper_name}"
                + (f" on {it['ontology_match_type']}" if it.get("ontology_match_type") else ""), XSD.string))

        self.add(dec, PROV.wasGeneratedBy, self.source_run(it.get("mapping_source")))
        # confidence in the decision: the mapping judge's, when it reviewed this item
        conf = None
        for grp in ent["groups"]:
            for rv in self.reviews.get(grp["id"].lower()) or []:
                if rv.get("judge") == "mapping" and rv.get("verdict") in ("pass", "flag"):
                    conf = rv.get("confidence")
        if conf is None and isinstance(it.get("score"), (int, float)) and 0 <= it["score"] <= 1:
            conf = it["score"]
        if isinstance(conf, (int, float)):
            self.add(dec, NER.decisionConfidence, self.dec(round(float(conf), 4)))
        self.label(dec, ref[1])

    # ---- reviews ------------------------------------------------------------
    def judge_agent(self, judge: str) -> URIRef:
        model = self.judge_models.get(judge)
        label = f"judge: {judge}" + (f" ({model})" if model else "")
        cls = NER.PipelineAgent if judge in ("script", "combiner-script") or judge.endswith("_script") \
            else NER.LanguageModelAgent
        version = str(model).removeprefix("llm:").removeprefix("script:") if model else None
        # associated with this judge's own activity (judge_activity), not the ensemble
        # step, whose agent is the deterministic combine
        return self.agent(f"judge:{judge}", cls, label, version=version)

    def judge_activity(self, judge: str) -> URIRef:
        """One AutomaticValidationActivity per panel member: its model (agent + version),
        its mode, and the exact prompt it followed (ner:PromptArtifact, by hash). The
        ensemble step (judge_run) was informed by every one of them."""
        if not hasattr(self, "_judge_acts"):
            self._judge_acts = {}
        if judge in self._judge_acts:
            return self._judge_acts[judge]
        spec = ((self.result.get("judge_ensemble") or {}).get("judges") or {}).get(judge) or {}
        node = self.mint("run", f"{self.run_id}|judge|{judge}")
        self.add(node, RDF.type, NER.AutomaticValidationActivity)
        self.label(node, f"judge: {judge}")
        self.add(node, NER.runIdentifier, self.lit(f"{self.run_id}-judge-{judge}", XSD.string))
        dim = spec.get("dimension") or judge
        self.add(node, NER.taskType, self.lit(f"judge ({dim})", XSD.string))
        self.add(node, PROV.wasAssociatedWith, self.judge_agent(judge))
        self.add(self.judge_run, PROV.wasInformedBy, node)
        notes = []
        if spec.get("mode"):
            notes.append(f"mode: {spec['mode']}")
        if spec.get("critical"):
            notes.append("critical: a fail is a gate, not a vote")
        if spec.get("weight") is not None:
            notes.append(f"weight {spec['weight']}")
        if notes:
            self.add(node, RDFS.comment, Literal("; ".join(notes)))
        if spec.get("prompt"):
            self.add(node, NER.usedPrompt, self.prompt_node(spec["prompt"], spec.get("prompt_sha256")))
        self._judge_acts[judge] = node
        return node

    def prompt_node(self, path: str, sha: Optional[str]) -> URIRef:
        node = self.mint_global("agent", key=f"prompt:{path}:{sha or 'unhashed'}")
        if (node, RDF.type, NER.PromptArtifact) not in self.g:
            self.add(node, RDF.type, NER.PromptArtifact)
            self.label(node, path)
            if sha:
                self.add(node, NER.promptHash, self.lit(sha, XSD.string))
            self.add(node, NER.promptVersion, self.lit(f"structsense {SKILL_VERSION}", XSD.string))
        return node

    def build_judge_provenance(self):
        """What the ensemble CHANGED, as ner:ChangeRecords triggered by the step that
        made the change: relabels, tier changes, key renames, demoted mappings,
        dropped items and dropped claims — each with before/after values and the
        judge review that licensed it."""
        block = self.result.get("judge_ensemble") or {}
        report = block.get("report") or {}
        if not block:
            return
        self.add(self.judge_run, PROV.wasAssociatedWith, self.combine_agent())
        self.add(self.judge_run, RDFS.comment, Literal(
            "Deterministic aggregation: critical fails are gates, mapping fails demote, uncontested "
            "suggestions apply; judge_score = sum(w*conf*s(verdict))/sum(w)."))
        combiner = block.get("combiner")
        comb_run = self.combiner_run()[0]

        n = 0

        def record(cls, local: str, *, entity=None, field=None, old=None, new=None, reason=None,
                   by=None, activity=None, gid=None):
            nonlocal n
            n += 1
            cr = self.mint("change", f"{local}|{n}")
            self.add(cr, RDF.type, cls)
            self.label(cr, cls.split("/")[-1])
            self.add(cr, RDFS.comment, Literal(local))
            if entity is not None:
                self.add(cr, NER.changedEntity, entity)
                self.add(entity, NER.hasChangeRecord, cr)
            for prop, val in ((NER.changedField, field), (NER.oldLiteralValue, old), (NER.newLiteralValue, new),
                              (NER.changeReason, reason)):
                if val is not None and val != "":
                    self.add(cr, prop, self.lit(str(val) if not isinstance(val, list) else "; ".join(map(str, val)),
                                                XSD.string))
            self.add(cr, NER.triggeredByActivity, activity or self.judge_run)
            if by:
                self.add(cr, RDFS.comment, Literal(f"licensed by the {by} judge"))
            if gid is not None:  # the reviews behind the change (created here for dropped items)
                for rv in self.reviews.get(gid.lower()) or []:
                    if rv.get("judge") != "combine" and (not by or rv.get("judge") in (by if isinstance(by, list) else [by])):
                        self.add(cr, NER.hasReviewDecision, self.review_node(gid, rv))
            self.counts["change_records"] += 1
            return cr

        field_class = {"label": NER.ClassificationChangedChange, "tier": NER.MappingChangedChange,
                       "normalized_key": NER.CanonicalFormChangedChange}
        for fx in report.get("fixes_applied") or []:
            gid = fx.get("new_id") or fx.get("id")
            ent = self.entity_by_group_id.get(str(gid).lower()) or self.entity_by_group_id.get(str(fx.get("id")).lower())
            record(field_class.get(fx.get("field"), NER.ChangeRecord), f"{fx.get('id')}|{fx.get('field')}",
                   entity=ent["node"] if ent else None, field=fx.get("field"), old=fx.get("from"),
                   new=fx.get("to"), reason=f"{fx.get('field')} fixed by {fx.get('applied_by', 'script')}",
                   by=fx.get("licensed_by"), gid=gid)
        for dm in report.get("demoted") or []:
            ent = self.entity_by_group_id.get(str(dm["id"]).lower())
            record(NER.MappingRemovedChange, f"{dm['id']}|mapping", entity=ent["node"] if ent else None,
                   field="ontology_id", old=dm.get("from"), new="unmapped", reason=dm.get("reason"),
                   by="mapping", gid=dm["id"])
        for dr in report.get("dropped") or []:
            # the entity is not in the graph — the change record, its reviews and the
            # offsets it had are all that remains, and that is the point
            cr = record(NER.MentionRemovedChange, f"{dr['id']}|dropped", field="entity",
                        old=f"{dr.get('surface')} ({dr.get('label')}) at {dr.get('offsets')}",
                        new="removed", reason=dr.get("reason"), by=dr.get("by"), gid=dr["id"])
            self.add(cr, PROV.wasDerivedFrom, self.docv)
        for dc in (report.get("claims") or {}).get("dropped") or []:
            record(NER.CausalRelationRemovedChange if str(dc.get("id", "")).startswith("rel") else NER.ChangeRecord,
                   f"{dc.get('id')}|claim", field="claim", old=dc.get("id"), new="removed",
                   reason=dc.get("reason"), by="claims")
        for fx in (report.get("claims") or {}).get("fixed") or []:
            record(NER.CausalRelationChange, f"{fx.get('id')}|{fx.get('field')}", field=fx.get("field"),
                   old=fx.get("from"), new=fx.get("to"), reason="claim flag corrected", by="claims")
        for fx in (combiner or {}).get("fixes") or []:
            if fx.get("applied"):
                ent = self.entity_by_group_id.get(str(fx.get("id")).lower())
                record(field_class.get(fx.get("field"), NER.ChangeRecord), f"{fx.get('id')}|{fx.get('field')}|combiner",
                       entity=ent["node"] if ent else None, field=fx.get("field"), old=fx.get("from"),
                       new=fx.get("to"), reason=fx.get("evidence"), by=fx.get("licensed_by"),
                       activity=comb_run, gid=fx.get("id"))
        if report:
            vr = self.mint("validation_report", self.run_id)
            self.add(vr, RDF.type, NER.ValidationReport)
            summary = (f"judge ensemble: {len(report.get('dropped') or [])} dropped, "
                       f"{len(report.get('demoted') or [])} demoted, {len(report.get('fixes_applied') or [])} fixed, "
                       f"{len(report.get('needs_review') or [])} needed review, "
                       f"{len((report.get('claims') or {}).get('dropped') or [])} claims dropped")
            self.label(vr, "judge ensemble report")
            self.add(vr, RDFS.comment, Literal(summary))
            self.add(vr, PROV.wasGeneratedBy, self.judge_run)
            self.add(self.snapshot, NER.hasValidationReport, vr)

    def review_node(self, gid: str, rv: dict) -> URIRef:
        """One judge's verdict on one item, as a ner:ReviewDecision: its dimension
        (the judge), status, confidence, reason, the judge agent and the judge's own
        activity. Idempotent — the same (item, judge) is one node."""
        judge = rv.get("judge") or "judge"
        r = self.mint("review", f"{gid}|{judge}")
        if (r, RDF.type, NER.ReviewDecision) in self.g:
            return r
        verdict = rv.get("verdict") or "pass"
        status = self.cfg.review_status.get(verdict, "unreviewed")
        if verdict == "flag" and rv.get("applied_fix"):
            status = self.cfg.review_status.get("flag_fixed", status)
        self.add(r, RDF.type, NER.ReviewDecision)
        self.add(r, NER.reviewDimension, self.lit(judge))
        self.add(r, NER.reviewStatus, NER[f"review-status/{status}"])
        conf = rv.get("confidence")
        if isinstance(conf, (int, float)) and 0 <= conf <= 1:
            self.add(r, NER.reviewConfidence, self.dec(round(float(conf), 4)))
        self.add(r, NER.reviewerDecisionRaw, self.lit(f"{judge}: {verdict}"))
        if rv.get("reason"):
            self.add(r, NER.reviewComment, self.lit(str(rv["reason"])))
        self.add(r, PROV.wasAttributedTo, self.judge_agent(judge))
        self.add(r, PROV.wasGeneratedBy, self.judge_activity(judge))
        self.label(r, judge)
        self.counts["review_decisions"] += 1
        return r

    def combined_review(self, gid: str, per_judge: list[URIRef], items: list[dict]) -> Optional[URIRef]:
        """The ensemble's own decision on an item (dimension "ensemble-combined"): the
        aggregated judge_score as its confidence, derived from every per-judge decision,
        made by the combine step — or by the combiner, if it settled this item."""
        if not per_judge:
            return None
        r = self.mint("review", f"{gid}|ensemble-combined")
        if (r, RDF.type, NER.ReviewDecision) in self.g:
            return r
        report = (self.result.get("judge_ensemble") or {}).get("report") or {}
        fixed = {str(f.get("new_id") or f.get("id")).lower() for f in report.get("fixes_applied") or []} \
            | {str(d.get("id")).lower() for d in report.get("demoted") or []}
        combiner = (self.result.get("judge_ensemble") or {}).get("combiner") or {}
        by_combiner = any(str(f.get("id")).lower() == gid.lower() and f.get("applied")
                          for f in combiner.get("fixes") or [])
        status = "corrected" if gid.lower() in fixed or by_combiner else "accepted"
        score = next((it.get("judge_score") for it in items if isinstance(it.get("judge_score"), (int, float))), None)
        self.add(r, RDF.type, NER.ReviewDecision)
        self.add(r, NER.reviewDimension, self.lit("ensemble-combined"))
        self.add(r, NER.reviewStatus, NER[f"review-status/{status}"])
        if score is not None:
            self.add(r, NER.reviewConfidence, self.dec(round(float(score), 4)))
        self.add(r, NER.reviewerDecisionRaw, self.lit(
            f"ensemble: {status}" + (f", judge_score {round(float(score), 4)}" if score is not None else "")))
        remark = next((it.get("remarks") for it in items if it.get("remarks")), None)
        if remark:
            self.add(r, NER.reviewComment, self.lit(str(remark)))
        for pj in per_judge:
            self.add(r, PROV.wasDerivedFrom, pj)
        if by_combiner:
            run, agent = self.combiner_run()
        else:
            run, agent = self.judge_run, self.combine_agent()
        self.add(r, PROV.wasGeneratedBy, run)
        self.add(r, PROV.wasAttributedTo, agent)
        self.label(r, "ensemble decision")
        self.counts["review_decisions"] += 1
        return r

    def combine_agent(self) -> URIRef:
        return self.agent("pipeline:judge_combine", NER.PipelineAgent,
                          "scripts/judge_combine.py (deterministic aggregation)", version=SKILL_VERSION)

    def combiner_run(self) -> tuple[Optional[URIRef], Optional[URIRef]]:
        """The LLM combiner step, if it ran (only for needs_review items)."""
        if hasattr(self, "_comb"):
            return self._comb
        combiner = (self.result.get("judge_ensemble") or {}).get("combiner")
        if not combiner:
            self._comb = (None, None)
            return self._comb
        run = self.mint("run", f"{self.run_id}|judge|combiner")
        self.add(run, RDF.type, NER.AutomaticValidationActivity)
        self.label(run, "judge combiner")
        self.add(run, RDFS.comment, Literal("Resolves needs_review items only, choosing among the judges' suggestions."))
        self.add(run, NER.runIdentifier, self.lit(f"{self.run_id}-judge-combiner"))
        self.add(run, NER.taskType, self.lit("judge combiner"))
        self.add(run, PROV.wasInformedBy, self.judge_run)
        model = combiner.get("model")
        agent = self.agent(f"judge:combiner:{model or 'unrecorded'}", NER.LanguageModelAgent,
                           f"judge combiner ({model or 'model not recorded'})", version=model)
        self.add(run, PROV.wasAssociatedWith, agent)
        if combiner.get("prompt"):
            self.add(run, NER.usedPrompt, self.prompt_node(combiner["prompt"], combiner.get("prompt_sha256")))
        t = as_datetime(combiner.get("applied_at"))
        if t is not None:
            self.add(run, PROV.endedAtTime, t)
        self._comb = (run, agent)
        return self._comb

    def emit_reviews(self, node: URIRef, slug: str, ids: Iterable[str], ent: Optional[dict] = None):
        """Attach every judge's decision, and the ensemble's combined decision, to the
        entity AND to each mention's current annotation version (the reading they judged)."""
        for gid in ids:
            revs = self.reviews.get(gid.lower()) or []
            per_judge = [self.review_node(gid, rv) for rv in revs if rv.get("judge") != "combine"]
            grp = next((g for g in (ent or {}).get("groups", []) if g["id"].lower() == gid.lower()), None)
            combined = self.combined_review(gid, per_judge, grp["items"] if grp else [])
            targets = [node] + list(((ent or {}).get("avs") or {}).get(gid, []))
            for t in targets:
                for r in per_judge + ([combined] if combined is not None else []):
                    self.add(t, NER.hasReviewDecision, r)

    # ---- resources ----------------------------------------------------------
    def build_resources(self, resources: list[dict]):
        for res in resources:
            rtype = res.get("type") or "Tool"
            gid = f"{res['name']}|{rtype}"
            grp = {"kind": "entity", "surface": res["name"], "label": rtype, "id": gid,
                   "surf_key": "name", "items": [res]}
            plan = self.plan_for(gid)
            cls, note = self.resolve_class(rtype, "entity", plan)
            key = plan.get("normalized_key") or normalize_key(res["name"], self.synonyms)
            slug = self.unique_slug("entity-" + key.replace("_", "-")[:70])
            ent = {"key": key, "node": self.mint_global("entity", key=key), "slug": slug, "classes": {cls},
                   "groups": [grp], "from_plan": bool(plan), "notes": [note] if note else [],
                   "label": plan.get("normalized_label") or res["name"], "plans": [plan]}
            if key in self.entities_by_key:
                ent = self.entities_by_key[key]
                ent["groups"].append(grp)
                continue
            self.entities_by_key[key] = ent
            self.entity_by_group_id[gid.lower()] = ent
            # Resource concepts arrive as mapped_target_concept[] rather than
            # per-mention ontology_id; lift the tool-provenanced ones.
            if res.get("concept_mapping_provenance") == "tool":
                for c in res.get("mapped_target_concept") or []:
                    res.setdefault("_concepts", []).append(c)
            self.emit_resource(ent, res)

    def emit_resource(self, ent: dict, res: dict):
        node = ent["node"]
        self.emit_entity_core(ent)
        if res.get("description"):
            self.add(node, RDFS.comment, Literal(f"description: {res['description']}"))
        url = res.get("url")
        if isinstance(url, str) and re.match(r"^https?://\S+$", url):
            self.add(node, RDFS.seeAlso, URIRef(url))
        m = self.mint("mention", f"{ent['key']}|1")
        self.add(node, NER.hasMention, m)
        self.add(m, RDF.type, NER.EntityMention)
        self.add(m, NER.surfaceForm, self.lit(res["name"], XSD.string))
        self.label(m, res["name"])
        self.add(m, NER.partOfDocumentVersion, self.docv)
        self.source_agent(res.get("source_model"))
        self.counts["mentions"] += 1
        emitted = 0
        for c in res.get("_concepts") or []:
            ref = concept_ref(str(c.get("id") or ""), c.get("ontology"), self.cfg, self.registry)
            if not ref:
                continue
            if ref[2] == "UNREGISTERED":
                continue
            concept = self.concept_node(ref, {"ontology_label": c.get("label")})
            tier = ent["plans"][0].get("skos_tier") or self.cfg.default_tier
            tier = _TIER_ALIASES.get(tier, tier) if tier not in MATCH_TIERS else tier
            self.add(node, NER.resolvedToConcept, concept)
            self.add(node, MATCH_TIERS.get(tier, MATCH_TIERS[self.cfg.default_tier]), URIRef(ref[0]))
            emitted += 1
        if not emitted:
            self.add(node, RDFS.comment, Literal(
                f"No tool-verified ontology mapping; checked via {self.mapper_name} on {self.date}."))
        for kind, names in (res.get("mentions") or {}).items():
            cls = self.cfg.secondary_classes.get(kind, self.cfg.secondary_default)
            for name in names or []:
                other = self.secondary_resource(name, cls)
                self.add(node, SKOS.related, other)

    def emit_entity_core(self, ent: dict):
        node = ent["node"]
        for cls in sorted(ent["classes"]):
            self.add(node, RDF.type, NER[cls])
        self.add(node, RDF.type, NER.NamedEntity)
        self.add(node, NER.normalizedEntityKey, self.lit(ent["key"], XSD.string))
        self.entity_name(node, ent)
        self.add(node, PROV.hadPrimarySource, self.pub)
        self.add(node, PROV.wasGeneratedBy, self.run)
        self.add(self.snapshot, PROV.hadMember, node)
        for note in dict.fromkeys(ent["notes"]):
            self.add(node, RDFS.comment, Literal(note))
        self.counts["entities"] += 1

    def secondary_resource(self, name: str, cls: str) -> URIRef:
        key = normalize_key(name, self.synonyms)
        if key in self.entities_by_key:
            return self.entities_by_key[key]["node"]
        slug = self.unique_slug("entity-" + key.replace("_", "-")[:70])
        ent = {"key": key, "node": self.mint_global("entity", key=key), "slug": slug, "classes": {cls}, "groups": [],
               "from_plan": False, "notes": ["Mentioned by a primary resource; not itself described."],
               "label": name, "plans": [{}]}
        self.entities_by_key[key] = ent
        self.emit_entity_core(ent)
        m = self.mint("mention", f"{key}|1")
        self.add(ent["node"], NER.hasMention, m)
        self.add(m, RDF.type, NER.EntityMention)
        self.add(m, NER.surfaceForm, self.lit(name, XSD.string))
        self.label(m, name)
        self.add(m, NER.partOfDocumentVersion, self.docv)
        self.add(ent["node"], RDFS.comment, Literal(
            f"No tool-verified ontology mapping; checked via {self.mapper_name} on {self.date}."))
        self.counts["mentions"] += 1
        return ent["node"]

    # ---- kg_plan edges and causal module -------------------------------------
    def by_key(self, key: Optional[str], ctx: str) -> Optional[URIRef]:
        if not key:
            return None
        ent = self.entities_by_key.get(self.plan_key_aliases.get(key, key))
        if ent is None:
            self.warnings.append(f"{ctx}: target key {key!r} matches no entity; edge skipped")
            return None
        return ent["node"]

    def build_plan_edges(self):
        for gid, plan in (self.plan.get("entities") or {}).items():
            ent = self.entity_by_group_id.get(gid.lower()) or self.entity_by_group_id.get(reading_form(gid).lower())
            if ent is None:
                self.warnings.append(f"kg_plan entry {gid!r} matches no extracted item; ignored")
                continue
            relations = list(plan.get("relations") or [])
            if plan.get("broader_key"):
                relations.append({"predicate": "broader", "target_key": plan["broader_key"],
                                  "evidence": plan.get("evidence")})
            for field, pred in (("related_keys", "related"), ("see_also_keys", "see_also"),
                                ("uses_keys", "uses"), ("derived_from_keys", "derived_from")):
                relations.extend({"predicate": pred, "target_key": k, "evidence": plan.get("evidence")}
                                 for k in plan.get(field) or [])
            extra = {"broader": SKOS.broader, "related": SKOS.related, "see_also": RDFS.seeAlso,
                     "uses": PROV.used, "derived_from": PROV.wasDerivedFrom}
            for rel in relations:
                name = str(rel.get("predicate") or "")
                pred = self.cfg.relations.get(name) or self.cfg.relations_by_id.get(name) or extra.get(name)
                tgt = self.by_key(rel.get("target_key"), gid)
                if pred is None or tgt is None or tgt == ent["node"]:
                    continue
                evidence = {"text": rel.get("evidence"), **{k: rel[k] for k in
                            ("start", "end", "negated", "modality", "context", "time", "condition") if k in rel}}
                self.emit_relation_assertion(ent["node"], pred, tgt, evidence)

    def emit_relation_assertion(self, src, pred, tgt, evidence: dict):
        quote = evidence.get("text") or ""
        if not quote or (self.source_text and quote not in self.source_text):
            self.warnings.append("relation omitted: missing or ungrounded evidence quote")
            return
        identity = json.dumps([str(src), str(pred), str(tgt), evidence], sort_keys=True, default=str)
        node = self.mint("relation_assertion", identity)
        self.add(node, RDF.type, NER.RelationAssertion)
        self.label(node, f"{self.g.value(src, RDFS.label)} → {self.g.value(tgt, RDFS.label)}")
        self.add(node, NER.assertionSubject, src)
        self.add(node, NER.assertionPredicate, pred)
        self.add(node, NER.assertionObject, tgt)
        self.add(node, NER.evidenceText, self.lit(quote))
        self.add(node, PROV.hadPrimarySource, self.pub)
        self.add(src, NER.hasRelationAssertion, node)
        negated = bool(evidence.get("negated"))
        self.add(node, NER.assertionNegated, Literal(negated, datatype=XSD.boolean))
        modality = str(evidence.get("modality") or "asserted")
        self.add(node, NER.assertionModality, self.lit(modality))
        for field in ("context", "time", "condition"):
            if evidence.get(field) is not None:
                self.add(node, NER.assertionContext, self.lit(f"{field}: {evidence[field]}"))
        for mention in self.g.objects(src, NER.hasMention):
            a = self.g.value(mention, NER.documentStartOffset)
            b = self.g.value(mention, NER.documentEndOffset)
            if a is not None and b is not None and int(a) == evidence.get("start") and int(b) == evidence.get("end"):
                self.add(node, NER.hasEvidenceMention, mention)
        if not negated and modality == "asserted" and not any(evidence.get(f) for f in ("context", "time", "condition")):
            self.add(src, pred, tgt)
        self.counts["relation_assertions"] += 1

    def build_extracted_claims(self) -> list[dict]:
        """Relations the EXTRACTOR stated (per-mention `relations` / `broader`, cns-cells
        `cell_context`) become edges between entities; its `causal_relations` join the
        causal module. Claims the claims judge failed are skipped; its corrections are
        applied. Returns the causal claims, converted to kg_plan form."""
        from relations import extracted_claims
        report = ((self.result.get("judge_ensemble") or {}).get("report") or {}).get("claims") or {}
        dropped = {str(d.get("id")).lower() for d in report.get("dropped") or []}
        fixes: dict[str, dict] = {}
        for fx in report.get("fixed") or []:
            fixes.setdefault(str(fx.get("id")).lower(), {})[fx.get("field")] = fx.get("to")
        xc = extracted_claims(self.result)
        for u in xc["unresolved"]:
            self.warnings.append(f"extracted relation not written: {u['source']} --{u['predicate']}--> "
                                 f"{u['target']!r}: {u['why']}")
        for r in xc["relations"]:
            if r["id"].lower() in dropped:
                self.counts["extracted_relations_dropped"] += 1
                continue
            src, tgt = self.entity_by_group_id.get(r["source"].lower()), self.entity_by_group_id.get(r["target"].lower())
            if not src or not tgt or src is tgt:
                continue
            pred = SKOS.broader if r["predicate"] == "broader" else self.cfg.relations.get(r["predicate"])
            if pred is None:
                continue
            for evidence in r.get("evidence_records") or []:
                self.emit_relation_assertion(src["node"], pred, tgt["node"], evidence)
            self.counts["extracted_relations"] += 1
            if self.profile == "full":  # the claims verdict on this edge hangs off its subject
                for rv in self.reviews.get(r["id"].lower()) or []:
                    self.add(src["node"], NER.hasReviewDecision, self.review_node(r["id"], rv))
        causal = []
        for c in xc["causal_relations"]:
            if c["id"].lower() in dropped:
                self.counts["extracted_causal_dropped"] += 1
                continue
            cause, effect = self.entity_by_group_id.get(c["cause"].lower()), self.entity_by_group_id.get(c["effect"].lower())
            if not cause or not effect or cause is effect:
                continue
            cr = {k: v for k, v in c.items() if k not in ("cause", "effect", "mediators", "moderators", "confounders", "origin")}
            cr.update({"cause_key": cause["key"], "effect_key": effect["key"],
                       "mediator_keys": [self.entity_by_group_id[g.lower()]["key"] for g in c.get("mediators") or []
                                         if g.lower() in self.entity_by_group_id],
                       "moderator_keys": [self.entity_by_group_id[g.lower()]["key"] for g in c.get("moderators") or []
                                          if g.lower() in self.entity_by_group_id],
                       "confounder_keys": [self.entity_by_group_id[g.lower()]["key"] for g in c.get("confounders") or []
                                           if g.lower() in self.entity_by_group_id]})
            cr.update(fixes.get(c["id"].lower(), {}))
            if isinstance(cr.get("evidence_basis"), str):
                cr["evidence_basis"] = [cr["evidence_basis"]]
            causal.append(cr)
        return causal

    def normalize_causal(self, cr: dict, ver) -> dict:
        """Causal values -> the ontology's own vocabulary: declared as is, else through
        ttl_config causal_value_aliases, else dropped (rdfs:comment keeps the raw word)."""
        aliases = self.cfg.raw.get("causal_value_aliases") or {}
        schemes = {"type": "causal-type", "polarity": "causal-polarity", "modality": "causal-modality",
                   "directness": "causal-directness", "evidence_basis": "causal-basis"}
        for field, scheme in schemes.items():
            vals = cr.get(field)
            if vals is None:
                continue
            many = isinstance(vals, list)
            out = []
            for v in (vals if many else [vals]):
                raw = str(v).strip()
                w = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
                w = w if w in self.vocab.get(scheme, ()) else (aliases.get(field) or {}).get(w, w)
                if w in self.vocab.get(scheme, ()):
                    out.append(w)
                elif raw:
                    self.add(ver, RDFS.comment, Literal(f"{field} as extracted: {raw!r} (not in ner:{scheme}/*)"))
                    self.warnings.append(f"causal {field} {raw!r} is not in ner:{scheme}; not written")
            cr[field] = out if many else (out[0] if out else None)
        if isinstance(cr.get("evidence_basis"), str):
            cr["evidence_basis"] = [cr["evidence_basis"]]
        return cr

    def build_causal(self, extra: Optional[list[dict]] = None):
        rel_nodes: dict[str, URIRef] = {}
        planned = list(self.plan.get("causal_relations") or [])
        seen = {(c.get("cause_key"), c.get("effect_key")) for c in planned}
        # extraction's claims, unless kg_plan already states the same cause -> effect
        planned += [c for c in extra or [] if (c.get("cause_key"), c.get("effect_key")) not in seen]
        for cr in planned:
            rid = str(cr.get("id") or f"rel-{len(rel_nodes) + 1}")
            cause = self.by_key(cr.get("cause_key"), rid)
            effect = self.by_key(cr.get("effect_key"), rid)
            if cause is None or effect is None:
                self.warnings.append(f"causal relation {rid!r} dropped: cause/effect unresolved")
                continue
            slug = slugify(rid)
            rel, ver = self.mint("causal_relation", rid), self.mint("causal_relation_version", f"{rid}|1")
            self.add(rel, RDF.type, NER.CausalRelation)
            self.add(rel, NER.hasCause, cause)
            self.add(rel, NER.hasEffect, effect)
            for field, pred in (("mediator_keys", NER.hasMediator), ("moderator_keys", NER.hasModerator),
                                ("confounder_keys", NER.hasConfounder)):
                for k in cr.get(field) or []:
                    n = self.by_key(k, rid)
                    if n is not None:
                        self.add(rel, pred, n)
            self.add(rel, NER.hasCurrentCausalRelationVersion, ver)
            self.add(rel, NER.hasCausalRelationVersion, ver)
            self.add(rel, PROV.hadPrimarySource, self.pub)
            self.add(rel, PROV.wasGeneratedBy, self.run)
            label = f"causal: {cr.get('cause_key')} → {cr.get('effect_key')}"
            self.label(rel, label)
            self.add(ver, RDF.type, NER.CausalRelationVersion)
            self.add(ver, NER.versionOfCausalRelation, rel)
            self.add(ver, NER.relationRevisionNumber, self.lit(1, XSD.positiveInteger))
            # current since this extraction; a later re-extraction supersedes it (validUntil)
            asserted = as_datetime(self.run_meta.get("ended_at")) or as_datetime(self.generated_at)
            self.add(ver, PROV.generatedAtTime, asserted)
            self.add(ver, NER.validFrom, asserted)
            self.add(ver, NER.causalNegated, Literal(bool(cr.get("negated", False))))
            cr = self.normalize_causal(dict(cr), ver)
            bases = [b for b in (cr.get("evidence_basis") or []) if b]
            hypothetical = cr.get("hypothetical")
            if hypothetical is None:
                hypothetical = not any(b in self.cfg.interventional for b in bases)
            self.add(ver, NER.causalHypothetical, Literal(bool(hypothetical)))
            for field, vocab in (("type", "causal-type"), ("polarity", "causal-polarity"),
                                 ("modality", "causal-modality"), ("directness", "causal-directness")):
                if cr.get(field):
                    self.add(ver, NER[_causal_prop(field)], NER[f"{vocab}/{cr[field]}"])
            for b in bases:
                self.add(ver, NER.causalEvidenceBasis, NER[f"causal-basis/{b}"])
            if isinstance(cr.get("strength"), (int, float)):
                self.add(ver, NER.causalStrength, self.dec(cr["strength"]))
            evidence = cr.get("evidence") or "no evidence fragment recorded"
            self.add(ver, RDFS.comment, Literal(f"evidence: {evidence}"))
            self.label(ver, "causal relation v1")
            est = cr.get("effect_estimate")
            if isinstance(est, dict) and est.get("measure"):
                self.emit_estimate(ver, f"{rid}|1", est)
            if self.profile == "full":
                self.emit_reviews(rel, "entity-" + slug, [rid])
            rel_nodes[rid] = rel
            self.counts["causal_relations"] += 1
        for ch in self.plan.get("chains") or []:
            members = [rel_nodes[r] for r in ch.get("relation_ids") or [] if r in rel_nodes]
            if not members:
                self.warnings.append(f"causal chain {ch.get('id')!r} has no surviving relations; skipped")
                continue
            node = self.mint("causal_chain", str(ch.get("id") or f"chain-{len(members)}"))
            self.add(node, RDF.type, NER.CausalChain)
            self.label(node, ch.get("label") or "causal chain")
            self.add(node, PROV.hadPrimarySource, self.pub)
            for r in members:
                self.add(node, NER.hasChainRelation, r)
            for a, b in zip(members, members[1:]):
                self.add(a, NER.nextCausalRelation, b)
            self.counts["causal_chains"] += 1

    def emit_estimate(self, ver: URIRef, slug: str, est: dict):
        e = self.mint("effect_estimate", slug)
        self.add(ver, NER.hasEffectEstimate, e)
        self.add(e, RDF.type, NER.EffectEstimate)
        self.add(e, NER.effectMeasure, self.lit(str(est["measure"]), XSD.string))
        parts = [str(est["measure"])]
        for field, prop in (("value", NER.effectValue), ("p_value", NER.pValue),
                            ("ci_lower", NER.confidenceIntervalLower),
                            ("ci_upper", NER.confidenceIntervalUpper)):
            if est.get(field) is not None:
                d = self.dec(est[field])
                if d is not None:
                    self.add(e, prop, d)
                    parts.append(f"{field}={est[field]}")
        if est.get("unit"):
            self.add(e, NER.effectUnit, self.lit(str(est["unit"]), XSD.string))
        if isinstance(est.get("sample_size"), int) and est["sample_size"] >= 0:
            self.add(e, NER.sampleSize, self.lit(est["sample_size"], XSD.nonNegativeInteger))
            parts.append(f"n={est['sample_size']}")
        self.label(e, "effect estimate")
        if parts:
            self.add(e, RDFS.comment, Literal(", ".join(parts)))

    # ---- driver -------------------------------------------------------------
    def build(self) -> Graph:
        self.build_spine()
        groups = build_groups(self.result)
        self.build_entities(groups)
        resources = _resource_items(self.result)
        if resources:
            self.build_resources(resources)
        extracted_causal = self.build_extracted_claims()
        self.build_plan_edges()
        self.build_causal(extracted_causal)
        if self.profile == "full":
            self.build_judge_provenance()
        for prefix in sorted({self.registry.canonical(a) for a in self.ontology_versions} - {None}):
            ns = self.registry.namespace(prefix)
            if ns and not ns.startswith(str(OBO)):
                self.g.bind(re.sub(r"[^A-Za-z0-9_-]", "_", prefix), Namespace(ns))
        if not self.entities_by_key:
            self.warnings.append("no entities, key terms or resources found in the result")
        return self.g


def _causal_prop(field: str) -> str:
    return {"type": "causalRelationType", "polarity": "causalPolarity",
            "modality": "causalModality", "directness": "causalDirectness"}[field]


CONFIG_FILES = ("concept_mapping.json", "judges_config.json", "trusted_ontologes/priority.md",
                "default_ontology/ttl_config.json", "default_ontology/label_class_map.json",
                "default_ontology/key_synonyms.json", "default_ontology/named_entity_ontology.owl",
                "default_ontology/named_entity_shapes.ttl")


def config_hash(files: Iterable[str] = CONFIG_FILES) -> str:
    """sha256 over the configuration actually in effect (name + bytes of each file),
    so two runs with different settings are distinguishable (ner:configurationHash)."""
    h = hashlib.sha256()
    for rel in sorted(files):
        f = SKILL_DIR / rel
        h.update(rel.encode() + b"\0")
        h.update(f.read_bytes() if f.is_file() else b"<missing>")
    return "sha256:" + h.hexdigest()


def as_datetime(value) -> Optional[Literal]:
    """xsd:dateTime from an ISO-8601 string; None if absent or unparseable (never invented)."""
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return Literal(d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), datatype=XSD.dateTime)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_if_file(path) -> Optional[str]:
    try:
        return sha256_of(Path(str(path))) if path and Path(str(path)).is_file() else None
    except OSError:
        return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def result_to_ttl(result: dict, *, kg_plan: Optional[dict] = None,
                  ontology: Path = DEFAULT_ONTOLOGY, label_map: Path = DEFAULT_LABEL_MAP,
                  synonyms: Path = DEFAULT_SYNONYMS, paper_slug: Optional[str] = None,
                  kb_ns: str = DEFAULT_KB_NS, profile: str = "compact",
                  run_id: Optional[str] = None, checked_date: Optional[str] = None,
                  source_path: Optional[Path] = None,
                  media_type: Optional[str] = None,
                  ttl_config: Path = DEFAULT_TTL_CONFIG,
                  variant: Optional[str] = None) -> tuple[str, dict]:
    """Library entry point. Returns (turtle_text, report)."""
    if source_path:
        result = {**result, "source_metadata": {"source_path": str(Path(source_path).resolve()),
                                                **(result.get("source_metadata") or {})}}
    cfg = TtlConfig(ttl_config)
    result, n_dropped = drop_non_entities(result, cfg.raw.get("non_entity_filter") or {})
    declared, onto_version = load_ontology_info(ontology)
    syn = load_json(synonyms, {}) or {}
    _INVARIANT_PLURALS.clear()
    _INVARIANT_PLURALS.update(syn.get("invariant_words") or [])
    meta = result.get("source_metadata") or {}
    date = checked_date or dt.date.today().isoformat()
    slug = paper_slug or paper_slug_for(meta, fallback=json.dumps(meta, sort_keys=True) or "paper")
    digest = hashlib.sha1(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[:8]
    checksum = sha256_of(source_path) if source_path and Path(source_path).is_file() else meta.get("sha256")
    builder = TurtleBuilder(
        result, kg_plan=kg_plan, label_map=load_json(label_map, {}),
        synonyms=syn.get("synonyms", {}),
        declared=declared, ontology_version=onto_version, paper_slug=slug, kb_ns=kb_ns,
        profile=profile, run_id=run_id or f"structsense-{date}-{digest}",
        checked_date=date, source_checksum=checksum, media_type=media_type, cfg=cfg, variant=variant,
        source_text=load_source_text(source_path))
    graph = builder.build()
    if n_dropped:
        builder.counts["non_entity_mentions_dropped"] = n_dropped
    report = {"paper_slug": slug, "namespace": str(builder.EX), "profile": profile, "variant": builder.variant,
              "triples": len(graph), "counts": dict(builder.counts),
              "warnings": builder.warnings}
    return graph.serialize(format="turtle"), report


def default_ttl_path(result_path: Path) -> Path:
    stem = result_path.stem
    for suffix in (".final", ".judged", "_final", "_judged"):
        stem = stem.removesuffix(suffix)
    return result_path.with_name(stem + ".ttl")


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", type=Path, help="canonical result JSON (<stem>_final.json)")
    ap.add_argument("--kg-plan", type=Path, help="kg_plan.json (prompts/kg-plan.md) — a default step for NER")
    ap.add_argument("--no-kg-plan", action="store_true",
                    help="convert a NER result without a kg_plan deliberately (silences the warning)")
    ap.add_argument("--source", type=Path, help="source document, for its sha256 checksum")
    ap.add_argument("--out", type=Path, help="output .ttl (default <stem>.ttl)")
    ap.add_argument("--report", type=Path, help="write the conversion report JSON here")
    ap.add_argument("--profile", choices=["full", "compact"], default="compact")
    ap.add_argument("--entity-views", action="store_true",
                    help="also write <stem>.entities.json / .entities.ttl (regenerable any time with "
                         "python -m scripts.entity_view <stem>.ttl)")
    ap.add_argument("--paper-slug", help="override the paper namespace slug")
    ap.add_argument("--kb-ns", default=DEFAULT_KB_NS, help="base for per-paper IRIs")
    ap.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY)
    ap.add_argument("--label-map", type=Path, default=DEFAULT_LABEL_MAP)
    ap.add_argument("--synonyms", type=Path, default=DEFAULT_SYNONYMS)
    ap.add_argument("--ttl-config", type=Path, default=DEFAULT_TTL_CONFIG,
                    help="representation policy (default default_ontology/ttl_config.json)")
    ap.add_argument("--run-id")
    ap.add_argument("--variant", default=None,
                    help="which extraction of the paper this is (default: task_type:ner_domain from the "
                         "result, e.g. ner:cns-cells). Scopes per-run IRIs so two extractions of one "
                         "paper never share a mention, classification or review node")
    ap.add_argument("--started-at", help="ISO-8601 time the extraction run began (else result.run_metadata)")
    ap.add_argument("--ended-at", help="ISO-8601 time the extraction run ended (else result.run_metadata)")
    ap.add_argument("--publication-date", help="YYYY-MM-DD (else source_metadata.publication_date)")
    ap.add_argument("--date", help="date recorded in gap comments (default today)")
    args = ap.parse_args()

    result = json.loads(args.result.read_text())
    plan = json.loads(args.kg_plan.read_text()) if args.kg_plan else None
    if plan is None and not args.no_kg_plan and (result.get("entities") or result.get("key_terms")):
        print("warning: no --kg-plan. The KG plan is a default step for NER (prompts/kg-plan.md): without "
              "it, keys come from the ontologies/algorithm only and no coreference, finer classes or "
              "cross-sentence claims are represented. Pass --no-kg-plan if this is intended.", file=sys.stderr)
    rm = result.setdefault("run_metadata", {})
    if args.started_at:
        rm["started_at"] = args.started_at
    if args.ended_at:
        rm["ended_at"] = args.ended_at
    if args.publication_date:
        result.setdefault("source_metadata", {})["publication_date"] = args.publication_date
    ttl, report = result_to_ttl(
        result, kg_plan=plan, ontology=args.ontology, label_map=args.label_map,
        synonyms=args.synonyms, paper_slug=args.paper_slug, kb_ns=args.kb_ns,
        profile=args.profile, run_id=args.run_id, checked_date=args.date,
        source_path=args.source, ttl_config=args.ttl_config, variant=args.variant)
    out = args.out or default_ttl_path(args.result)
    out.write_text(ttl)
    if args.entity_views:  # one TTL per source by default; views on request
        from entity_view import write_entity_views
        report.update(write_entity_views(ttl, out))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    c = report["counts"]
    print(f"wrote {out}: {report['triples']} triples, {c.get('entities', 0)} entities, "
          f"{c.get('mentions', 0)} mentions, {c.get('mapped_concepts', 0)} mapped concepts, "
          f"{c.get('unmapped_entities', 0)} unmapped, {c.get('causal_relations', 0)} causal "
          f"relations, {c.get('review_decisions', 0)} review decisions", file=sys.stderr)
    for w in report["warnings"][:20]:
        print(f"  warning: {w}", file=sys.stderr)
    if len(report["warnings"]) > 20:
        print(f"  ... and {len(report['warnings']) - 20} more warnings", file=sys.stderr)
    print(f"next: python -m scripts.validate_ttl {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
