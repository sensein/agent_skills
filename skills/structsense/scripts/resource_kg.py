"""Resource extraction -> resource knowledge graph (BrainKB Resource Ontology, BKR).

The deliverable of a resource extraction is a BKR graph: per resource, what it is,
what it is claimed to apply to versus shown to apply to versus used on, what it
assumes, how it fails, what it needs and produces, who owns it, which versions
exist, and which paper says so — each statement tied to a verbatim quote. The
ontology is default_ontology/brainkb_resource_ontology.owl (the BKR standalone),
the policy shapes default_ontology/brainkb_resource_shapes.ttl, the extraction
contract schemas/bkr-resource-extraction.schema.json. Settings:
default_ontology/resource_kg_config.json.

    result JSON ─▶ records ─▶ ground ─▶ map concepts ─▶ provenance ─▶ convert ─▶ stubs ─▶ link
                  (BKR or     (source    (trusted ->     (paper +     (bkr_convert) (bkr_stubs) (NER
                   legacy)     text)      local/BioPortal) run)                                 publication)

  records     extracted_resources as a BKR list, a {"1": [...]} map, or the legacy
              structsense shape (name/type/category/target/mentions{...}) — the
              legacy fields are carried over by the crosswalk the ontology itself
              declares (bkr:structsenseField).
  ground      identifiers, versions, URL and licence must be stated in the source;
              every evidence quote must occur in it. Unsupported values are removed
              and listed in not_found_fields; a validated scope left with no evidence
              becomes a declared one. Nothing is invented to fill a gap.
  map         scope-dimension labels (species, anatomy, cell types, assays, ...) go
              through the same ConceptMapper as NER (concept_mapping.json). Only a
              tool hit publishes an IRI; an llm_judgment mapping never does.
  link        the record's source work IS the paper's ner:Publication node (same
              UUIDv5 as json_to_ttl mints), so the resource KG and the NER KG of a
              paper join on it; a resource is one node across papers (keyed on its
              normalised name), its records and claims stay per paper.

Usage:
    python -m scripts.resource_kg build result_final.json --source paper.pdf [--out x.ttl] [--map]
    python -m scripts.resource_kg validate x.ttl [--json report.json]
    python -m scripts.json_to_ttl result_final.json --source paper.pdf   # same build, auto-detected

validate exits 0 when the only SHACL violations are source-silence findings
(resource_kg_config.json source_silence_shapes: a paper that states no licence),
1 on any other violation, undeclared term or disconnected island, 2 on a parse error.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent
for _p in (str(SCRIPTS), str(SKILL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdflib  # noqa: E402
from rdflib import RDF, RDFS, XSD, BNode, Literal, Namespace, URIRef  # noqa: E402

import bkr_convert  # noqa: E402
import bkr_stubs  # noqa: E402

ONT_DIR = SKILL_DIR / "default_ontology"
DEFAULT_CONFIG = ONT_DIR / "resource_kg_config.json"
NER_ONTOLOGY = ONT_DIR / "named_entity_ontology.owl"
BKR = Namespace("https://brainkb.org/resource/")
NER = Namespace("https://brainkb.org/ner/")
SH = Namespace("http://www.w3.org/ns/shacl#")
DCT = Namespace("http://purl.org/dc/terms/")
OBO = Namespace("http://purl.obolibrary.org/obo/")
SCHEMA = Namespace("https://schema.org/")
PROV = Namespace("http://www.w3.org/ns/prov#")

DIMENSIONS = ("species", "anatomical_structures", "cell_types", "developmental_stages", "assays",
              "modalities", "conditions", "tasks", "variables", "topics")
LOOSE = ("topics", "tasks", "modalities", "species")   # record-level concept lists
LEGACY_TYPE = {"model": "model", "dataset": "dataset", "tool": "tool", "benchmark": "benchmark",
               "leaderboard": "leaderboard", "paper": "publication", "library": "software_library",
               "framework": "software_library", "corpus": "dataset", "software": "tool"}
LEGACY_MENTION_TYPE = {"datasets": "dataset", "benchmarks": "benchmark", "models": "model",
                       "papers": "publication", "tools": "tool", "leaderboards": "leaderboard"}


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text())
    for key in ("ontology", "shapes", "schema"):
        cfg[key] = str((Path(path).parent / cfg[key]).resolve())
    return cfg


def norm_name(name: str) -> str:
    """Resource identity key: case, punctuation and whitespace folded ('SCANPY' == 'Scanpy')."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.\- ]", " ", str(name).lower())).strip(" .-")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


# ---------------------------------------------------------------------------
# Records: whatever container shape the extractor produced -> BKR records
# ---------------------------------------------------------------------------

def is_resource_result(result: dict) -> bool:
    return bool(result.get("task_type") == "resource" or result.get("extracted_resources")
                or result.get("aligned_resources") or result.get("judge_resource"))


def raw_records(result: dict) -> list[dict]:
    container = (result.get("judge_resource") or result.get("aligned_resources")
                 or result.get("extracted_resources") or [])
    if isinstance(container, dict):
        return [r for group in container.values() for r in (group or []) if isinstance(r, dict)]
    return [r for r in container if isinstance(r, dict)]


def is_bkr_record(rec: dict) -> bool:
    return "extracted_type" in rec


def _concepts(labels: Iterable[Any]) -> list[dict]:
    out = []
    for x in labels:
        lab = (x.get("label") if isinstance(x, dict) else x)
        if isinstance(lab, str) and lab.strip():
            out.append({"label": lab.strip()})
    return out


def _split(value: Optional[str]) -> list[str]:
    return [p.strip() for p in re.split(r"[;,]", value or "") if p.strip()]


def from_legacy(rec: dict, index: int) -> tuple[dict, list[str]]:
    """The pre-BKR structsense resource record -> a BKR record, by the ontology's own
    crosswalk (bkr:structsenseField): category -> tasks, target/specific_target -> a
    declared scope's target labels, mapped_specific_target_concept (NCBITaxon) ->
    species, judge_score/remarks -> judge, identifiers/versions -> stable_identifiers /
    versions with their quotes as field evidence."""
    notes: list[str] = []
    raw_type = str(rec.get("type") or "").strip().lower()
    etype = LEGACY_TYPE.get(raw_type)
    if etype is None:
        notes.append(f"{rec.get('name')}: legacy type {rec.get('type')!r} has no BKR type; typed 'tool'")
        etype = "tool"
    out: dict[str, Any] = {"record_id": f"r{index}-{slug(rec.get('name', 'resource'))}",
                           "extracted_type": etype, "name": rec.get("name") or f"resource {index}"}
    for k in ("description", "url"):
        if rec.get(k):
            out[k] = rec[k]
    if rec.get("category"):
        out["tasks"] = _concepts(_split(rec["category"]) or [rec["category"]])
    scope: dict[str, Any] = {}
    if rec.get("target"):
        scope["target_label"] = rec["target"]
    if rec.get("specific_target"):
        scope["specific_target_label"] = rec["specific_target"]
    species = []
    for m in rec.get("mapped_specific_target_concept") or []:
        mc = (m or {}).get("mapped_target_concept") or {}
        label = (m or {}).get("specific_target") or mc.get("label")
        if not label:
            continue
        c: dict[str, Any] = {"label": label}
        oid = mc.get("id") or mc.get("ontology_id")
        if oid and (mc.get("ontology") or "").upper() == "NCBITAXON":
            tool = rec.get("concept_mapping_provenance") == "tool"
            c["mapped"] = [{"concept_id": oid if tool and not str(oid).startswith("http") else None,
                            "concept_iri": oid if tool and str(oid).startswith("http") else None,
                            "concept_label": mc.get("label"), "ontology": "NCBITaxon",
                            "status": "accepted" if tool else "proposed",
                            "method": "hybrid_retrieval" if tool else "llm_judgment",
                            "provenance_raw": rec.get("concept_mapping_provenance") or "llm_knowledge"}]
            species.append(c)
    if species:
        scope["species"] = species
    if scope:
        scope["evidence_level"] = "extracted_from_text"
        out["applicability"] = {"declared": [scope]}
    else:  # the legacy record had target fields and left them empty: say so
        out["not_found_fields"] = ["applicability"]
    evidence = []
    idents = []
    for i, claim in enumerate(rec.get("identifiers") or []):
        if isinstance(claim, dict) and claim.get("value"):
            idents.append({"value": str(claim["value"]), "scheme": _scheme(claim.get("scheme"))})
            q = (claim.get("evidence") or {}).get("quote")
            if q:
                evidence.append({"field": f"stable_identifiers[{len(idents) - 1}]", "quote": q})
            if claim.get("scheme") and _scheme(claim.get("scheme")) is None:
                # an archive prefix (DANDI, GEO, ...) is not an identifier scheme: keep it as
                # the registry the accession resolves through, and the value as written
                idents[-1]["scheme"] = "accession"
                idents[-1]["resolves_through"] = str(claim["scheme"])
    if idents:
        out["stable_identifiers"] = idents
    versions = []
    for claim in rec.get("versions") or []:
        if isinstance(claim, dict) and claim.get("value"):
            versions.append({"version": str(claim["value"])})
            q = (claim.get("evidence") or {}).get("quote")
            if q:
                evidence.append({"field": f"versions[{len(versions) - 1}]", "quote": q})
    if versions:
        out["versions"] = versions
    mentions = []
    ments = rec.get("mentions") or {}
    if isinstance(ments, dict):
        for key, names in ments.items():
            for n in names or []:
                if isinstance(n, str) and n.strip():
                    mentions.append({"name": n.strip(), "extracted_type": LEGACY_MENTION_TYPE.get(key)})
    if mentions:
        out["mentions"] = mentions
    if evidence:
        out["provenance"] = {"field_evidence": evidence}
    if rec.get("judge_score") is not None or rec.get("remarks"):
        out["judge"] = {"score": rec.get("judge_score"), "method": rec.get("judge_method") or "llm",
                        "remarks": rec.get("remarks")}
    return out, notes


_SCHEMES = {"doi": "DOI", "rrid": "RRID", "handle": "Handle", "ark": "ARK", "purl": "PURL", "url": "URL",
            "urn": "URN", "pmid": "PMID", "swhid": "SWHID", "isbn": "ISBN", "issn": "ISSN", "arxiv": "arXiv",
            "bibcode": "bibcode", "w3id": "w3id", "accession": "accession"}


def _scheme(s: Optional[str]) -> Optional[str]:
    return _SCHEMES.get(str(s or "").strip().lower())


def merge_records(records: list[dict]) -> list[dict]:
    """Records of one resource (same normalised name and type) from several chunks -> one.
    Lists are concatenated without duplicates, scalars keep the first value, and a field
    one chunk filled is no longer 'not found'."""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for rec in records:
        key = (norm_name(rec.get("name", "")), rec.get("extracted_type"))
        if key not in merged:
            merged[key] = copy.deepcopy(rec)
            order.append(key)
            continue
        cur = merged[key]
        for k, v in rec.items():
            if k in ("record_id", "not_found_fields"):
                continue
            if isinstance(v, list):
                seen = {json.dumps(x, sort_keys=True) for x in cur.get(k) or []}
                cur[k] = (cur.get(k) or []) + [x for x in v if json.dumps(x, sort_keys=True) not in seen]
            elif isinstance(v, dict) and isinstance(cur.get(k), dict):
                for kk, vv in v.items():
                    if isinstance(vv, list):
                        seen = {json.dumps(x, sort_keys=True) for x in cur[k].get(kk) or []}
                        cur[k][kk] = (cur[k].get(kk) or []) + [x for x in vv if json.dumps(x, sort_keys=True) not in seen]
                    elif cur[k].get(kk) in (None, "", {}):
                        cur[k][kk] = vv
            elif cur.get(k) in (None, "", [], {}):
                cur[k] = v
        cur["not_found_fields"] = sorted(set(cur.get("not_found_fields") or []) | set(rec.get("not_found_fields") or []))
    out = [merged[k] for k in order]
    for rec in out:  # a field one chunk found is not missing
        if rec.get("not_found_fields"):
            rec["not_found_fields"] = [f for f in rec["not_found_fields"]
                                       if rec.get(f.split(".")[0].split("[")[0]) in (None, "", [], {})]
    seen_ids: Counter = Counter()
    for rec in out:
        rid = rec.get("record_id") or slug(rec.get("name", "resource"))
        seen_ids[rid] += 1
        rec["record_id"] = rid if seen_ids[rid] == 1 else f"{rid}-{seen_ids[rid]}"
    return out


def to_records(result: dict) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    recs = []
    for i, rec in enumerate(raw_records(result), 1):
        if is_bkr_record(rec):
            recs.append(copy.deepcopy(rec))
        else:
            r, n = from_legacy(rec, i)
            recs.append(r)
            notes.extend(n)
    return merge_records(recs), notes


# ---------------------------------------------------------------------------
# Grounding: only what the source states
# ---------------------------------------------------------------------------

class Source:
    """The normalised source text (the same normalisation the AIT evidence check uses:
    NFKC, quotes/dashes folded, soft hyphens and line-break hyphenation removed)."""

    def __init__(self, text: str):
        from ait_evidence import Normalized
        self.doc = Normalized(text)
        self.lower = self.doc.text.lower()

    def has_quote(self, quote: str) -> bool:
        from ait_evidence import verify_sentence
        return verify_sentence(quote, self.doc, has_offsets=False)["status"] != "not_found"

    def states(self, value: str) -> bool:
        """`value` occurs as a complete token (not a prefix of another identifier/version)."""
        from ait_evidence import normalize
        v = normalize(str(value)).lower()
        if not v:
            return False
        return bool(re.search(r"(?<![\w.])" + re.escape(v) + r"(?!\w|\.\w)", self.lower))

    def names(self, label: str) -> bool:
        from ait_evidence import normalize
        v = normalize(str(label)).lower()
        return bool(v) and v in self.lower


def _url_forms(url: str) -> list[str]:
    u = re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")
    return [url.strip(), url.strip().rstrip("/"), u]


def _identifier_forms(ident: dict) -> list[str]:
    v = str(ident.get("value") or "").strip()
    forms = [v]
    if (ident.get("scheme") or "").upper() == "DOI" or re.match(r"^(https?://(dx\.)?doi\.org/|doi:)", v, re.I):
        forms.append(re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", v, flags=re.I))
    if (ident.get("scheme") or "").upper() == "RRID":
        forms.append(re.sub(r"^RRID:\s*", "", v, flags=re.I))
    if re.match(r"^https?://", v):
        forms.extend(_url_forms(v))
    return [f for f in dict.fromkeys(forms) if f]


def _evidence_lists(rec: dict) -> Iterable[tuple[str, list]]:
    prov = rec.get("provenance") or {}
    if prov.get("field_evidence") is not None:
        yield "provenance.field_evidence", prov["field_evidence"]
    for key in ("assumptions", "failure_modes", "benchmark_evidence"):
        for i, item in enumerate(rec.get(key) or []):
            if isinstance(item, dict) and item.get("evidence") is not None:
                yield f"{key}[{i}].evidence", item["evidence"]
    for kind, scopes in (rec.get("applicability") or {}).items():
        for i, sc in enumerate(scopes or []):
            if isinstance(sc, dict) and sc.get("evidence") is not None:
                yield f"applicability.{kind}[{i}].evidence", sc["evidence"]


def ground(records: list[dict], source_text: Optional[str], cfg: dict) -> dict:
    """Remove what the source does not state. Returns a report; records change in place."""
    gcfg = cfg.get("grounding") or {}
    report: dict[str, Any] = {"source_text": source_text is not None, "removed": [], "warnings": []}
    if source_text is None:
        report["warnings"].append("no source text: identifiers, versions, URLs, licences and quotes "
                                  "could not be checked against the document and were removed")
    src = Source(source_text) if source_text is not None else None
    verify = set(gcfg.get("verify_values") or [])

    def drop(rec, field, value, why):
        if value is not None:
            report["removed"].append({"record": rec.get("record_id"), "resource": rec.get("name"),
                                      "field": field, "value": value, "reason": why})
        nf = rec.setdefault("not_found_fields", [])
        base = field.split("[")[0]
        if base not in nf and rec.get(base) in (None, "", [], {}):
            nf.append(base)

    for rec in records:
        name = rec.get("name", "")
        if src is not None and not src.names(name) and not any(
                src.names(k) for k in bkr_stubs.keys_for(name)):
            report["warnings"].append(f"{name}: the resource name does not occur in the source text")
        if "stable_identifiers" in verify:
            kept = []
            for i, ident in enumerate(rec.get("stable_identifiers") or []):
                if src is not None and any(src.states(f) for f in _identifier_forms(ident)):
                    kept.append(ident)
                else:
                    drop(rec, f"stable_identifiers[{i}]", ident.get("value"), "identifier not stated in the source")
            if rec.get("stable_identifiers") is not None:
                rec["stable_identifiers"] = kept
                if not kept:
                    rec.pop("stable_identifiers")
                    drop(rec, "stable_identifiers", None, "no stated identifier left")
        if "versions" in verify:
            kept = []
            for i, v in enumerate(rec.get("versions") or []):
                if src is not None and src.states(v.get("version", "")):
                    kept.append(v)
                else:
                    drop(rec, f"versions[{i}]", v.get("version"), "version not stated in the source")
            if rec.get("versions") is not None:
                rec["versions"] = kept
                if not kept:
                    rec.pop("versions")
                    drop(rec, "versions", None, "no stated version left")
        for field in ("url", "license"):
            if field in verify and rec.get(field):
                forms = _url_forms(rec[field]) if field == "url" or rec[field].startswith("http") else [rec[field]]
                if src is None or not any(src.names(f) for f in forms):
                    value = rec.pop(field)
                    drop(rec, field, value, f"{field} not stated in the source")
        kept_mentions = []
        for m in rec.get("mentions") or []:
            if src is None or src.names(m.get("name", "")) or any(src.names(k) for k in bkr_stubs.keys_for(m.get("name", ""))):
                kept_mentions.append(m)
            else:
                drop(rec, "mentions", m.get("name"), "mentioned resource not named in the source")
        if rec.get("mentions") is not None:
            rec["mentions"] = kept_mentions
        if gcfg.get("verify_quotes", True):
            for path, items in _evidence_lists(rec):
                keep = []
                for ev in items:
                    if not isinstance(ev, dict) or not ev.get("quote"):
                        continue
                    if src is not None and src.has_quote(ev["quote"]):
                        ev.pop("start", None)   # model-computed offsets are never trusted;
                        ev.pop("end", None)     # the quote is the anchor
                        keep.append(ev)
                    else:
                        report["removed"].append({"record": rec.get("record_id"), "resource": name,
                                                  "field": path, "value": ev["quote"][:160],
                                                  "reason": "quote not found in the source"})
                items[:] = keep
        # BKR invariant 2: a validated scope cites its validation, else it is declared
        app = rec.get("applicability") or {}
        demoted = []
        for sc in app.get("validated") or []:
            if not sc.get("evidence") and not rec.get("benchmark_evidence"):
                demoted.append(sc)
        if demoted:
            app["validated"] = [s for s in app["validated"] if s not in demoted]
            for sc in demoted:
                sc["evidence_level"] = "documented_in_publication" if sc.get("evidence_level") in (
                    "empirically_validated", "benchmarked", None) else sc["evidence_level"]
            app.setdefault("declared", []).extend(demoted)
            report["warnings"].append(f"{name}: {len(demoted)} validated scope(s) with no surviving "
                                      "evidence demoted to declared")
        # rule: a language model's own mapping never carries an IRI (the label survives)
        for _dim, c in _concept_slots(rec):
            for m in c.get("mapped") or []:
                if m.get("method") == "llm_judgment" and (m.get("concept_iri") or m.get("concept_id")):
                    report["removed"].append({"record": rec.get("record_id"), "resource": name,
                                              "field": f"{_dim}.mapped", "value": m.get("concept_iri") or m.get("concept_id"),
                                              "reason": "llm_judgment mapping: IRI dropped, label kept"})
                    m.pop("concept_iri", None)
                    m.pop("concept_id", None)
                    if m.get("status") == "accepted":
                        m["status"] = "proposed"
    report["n_removed"] = len(report["removed"])
    return report


# ---------------------------------------------------------------------------
# Concept mapping: the same tool cascade as NER, never a model's IRI
# ---------------------------------------------------------------------------

_TIER_RELATION = {"exactMatch": "exact", "closeMatch": "close", "broadMatch": "broader",
                  "narrowMatch": "narrower", "relatedMatch": "related"}


def _concept_slots(rec: dict) -> Iterable[tuple[str, dict]]:
    for dim in LOOSE:
        for c in rec.get(dim) or []:
            yield dim, c
    for scopes in (rec.get("applicability") or {}).values():
        for sc in scopes or []:
            for dim in DIMENSIONS:
                for c in sc.get(dim) or []:
                    yield dim, c


def _method_of(m: dict) -> str:
    if m.get("alignment_method") == "curated_mapping":
        return "rule_based"
    src = str(m.get("mapping_source") or "")
    if src == "local_hybrid":
        return "hybrid_retrieval"
    if src == "bioportal" and m.get("ontology_match_type") == "bioportal_label":
        return "exact_lexical"
    return "normalized_lexical"


def map_concepts(records: list[dict], mapper, cfg: dict) -> dict:
    """Fill each concept's `mapped` from the ConceptMapper (concept_mapping.ConceptMapper)."""
    routing = _clean(cfg.get("concept_routing"))
    accepted_tiers = set(cfg.get("accepted_tiers") or ["exactMatch"])
    items, skipped = [], Counter()
    for rec in records:
        for dim, c in _concept_slots(rec):
            if any(m.get("concept_iri") and m.get("method") != "llm_judgment" for m in c.get("mapped") or []):
                continue  # already tool-mapped
            label = routing.get(dim)
            if label is None:
                skipped[dim] += 1
                continue
            items.append({"term": c["label"], "label": label, "_concept": c})
    report: dict[str, Any] = {"sent": len(items), "not_routed": dict(skipped), "accepted": 0,
                              "proposed": 0, "ambiguous": 0, "unmapped": 0}
    if not items:
        return report
    mapper.map_items(items, "term")
    from prefixes import PrefixRegistry
    reg = PrefixRegistry()
    for it in items:
        c = it["_concept"]
        if it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id"):
            iri = str(it["ontology_id"])
            if not iri.startswith("http"):
                iri = reg.expand(iri) or iri
            tier = it.get("match_tier") or "exactMatch"
            status = "accepted" if tier in accepted_tiers else "proposed"
            curie = reg.compact(iri) if iri.startswith("http") else None
            entry = {"concept_iri": iri, "concept_id": curie[0] if curie else None,
                     "concept_label": it.get("ontology_label"), "ontology": it.get("ontology"),
                     "relation": _TIER_RELATION.get(tier, "close"), "status": status,
                     "method": _method_of(it), "provenance_raw": "tool"}
            if isinstance(it.get("score"), (int, float)) and 0 <= it["score"] <= 1:
                entry["confidence"] = float(it["score"])
            c.setdefault("mapped", []).append({k: v for k, v in entry.items() if v is not None})
            report[status] += 1
        elif it.get("trusted_ambiguous"):
            cands = "; ".join(f"{a.get('ontology_label')} <{a.get('ontology_id')}>" for a in it["trusted_ambiguous"][:5])
            c.setdefault("mapped", []).append({"status": "ambiguous", "method": "normalized_lexical",
                                               "provenance_raw": "tool",
                                               "concept_label": f"ambiguous between: {cands}"})
            report["ambiguous"] += 1
        else:
            report["unmapped"] += 1
    try:
        report["mapper"] = mapper.meta()
    except Exception:  # meta is informational
        pass
    return report


# ---------------------------------------------------------------------------
# Provenance: which paper, which run
# ---------------------------------------------------------------------------

def _doi(meta: dict) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", str(meta.get("doi") or "").strip(), flags=re.I)


def paper_identity(result: dict, source_path: Optional[Path]) -> tuple[str, str, Optional[str]]:
    """(paper_id, publication IRI, DOI): the same derivation json_to_ttl uses, so the NER
    graph and the resource graph of one paper name its publication with one IRI."""
    from json_to_ttl import TtlConfig, paper_slug_for, sha256_of
    meta = dict(result.get("source_metadata") or {})
    if source_path and "source_path" not in meta:
        meta["source_path"] = str(Path(source_path).resolve())
    doi = _doi(meta)
    checksum = sha256_of(source_path) if source_path and Path(source_path).is_file() else meta.get("sha256")
    slug_ = paper_slug_for(meta, fallback=json.dumps(meta, sort_keys=True) or "paper")
    paper_id = str(meta.get("source_id") or doi or meta.get("source_path") or meta.get("sha256")
                   or checksum or slug_)
    tc = TtlConfig()
    kb = tc.iri_base if tc.iri_base.endswith("/") else tc.iri_base + "/"
    name = tc.paper_shared_name.format(kind="publication", paper=paper_id, variant="", local="1")
    return paper_id, kb + str(uuid.uuid5(tc.uuid_ns, name)), doi or None


def display_id(paper_id: str) -> str:
    """What the graph may SAY the paper is: a DOI or id as is, a local path by its file
    name only (ttl_config.json source_path: never the user's directory)."""
    return Path(paper_id).name if ("/" in paper_id or "\\" in paper_id) and not re.match(r"^10\.\d{4,}/", paper_id) else paper_id


def attach_provenance(records: list[dict], result: dict, paper_id: str, doi: Optional[str]) -> None:
    from json_to_ttl import SKILL_VERSION
    meta = result.get("source_metadata") or {}
    run = result.get("run_metadata") or {}
    work = {"identifier": doi or display_id(paper_id)}
    if meta.get("paper_title") or meta.get("title"):
        work["title"] = meta.get("paper_title") or meta.get("title")
    if doi:
        work["url"] = f"https://doi.org/{doi}"
    ts = run.get("ended_at") or run.get("started_at")
    for rec in records:
        prov = rec.setdefault("provenance", {})
        docs = prov.setdefault("source_documents", [])
        if not any((d.get("identifier") or "").lower().removeprefix("https://doi.org/")
                   in {paper_id.lower(), display_id(paper_id).lower(), (doi or "").lower()} for d in docs):
            docs.insert(0, dict(work))
        ext = prov.setdefault("extraction", {})
        ext.setdefault("pipeline", "structsense")
        ext.setdefault("pipeline_version", SKILL_VERSION)
        if run.get("extractor_model"):
            ext.setdefault("model_name", run["extractor_model"])
        if ts and re.match(r"^\d{4}-\d{2}-\d{2}T", str(ts)):
            ext.setdefault("timestamp", str(ts))
        attribute_claims(rec, work["identifier"])


def attribute_claims(rec: dict, paper: str) -> None:
    """Say which paper states each claim. A resource is one node across papers, so a
    scope, assumption, benchmark or quote hanging from it would otherwise be
    unattributable once two papers describe it. BKR has the properties
    (bkr:scopeAssertedIn, bkr:assumptionStatedIn, prov:hadPrimarySource on a benchmark
    result and on every evidence mention); default each to this record's paper."""
    def evidence(items):
        for ev in items or []:
            if isinstance(ev, dict):
                ev.setdefault("document", paper)
    evidence((rec.get("provenance") or {}).get("field_evidence"))
    for scopes in (rec.get("applicability") or {}).values():
        for sc in scopes or []:
            sc.setdefault("asserted_in", paper)
            evidence(sc.get("evidence"))
    for a in rec.get("assumptions") or []:
        a.setdefault("source", paper)
        evidence(a.get("evidence"))
    for f in rec.get("failure_modes") or []:
        evidence(f.get("evidence"))
    for b in rec.get("benchmark_evidence") or []:
        b.setdefault("source", paper)
        evidence(b.get("evidence"))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _scope_ids(records: list[dict], paper_id: str) -> None:
    """record_id is local to one extraction ('r1'); scope it by paper so two papers'
    records, scopes and assumptions never share an IRI. The paper is hashed: its id
    can be a local file path, which must not end up in a published key."""
    paper = "paper-" + hashlib.sha1(paper_id.encode()).hexdigest()[:12]
    for rec in records:
        rec["record_id"] = f"{paper}/{rec['record_id']}"


def _strip_nulls(x):
    if isinstance(x, dict):
        return {k: _strip_nulls(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_strip_nulls(v) for v in x]
    return x


def prepare(result: dict, source_text: Optional[str], *, mapper=None, cfg: Optional[dict] = None) -> dict:
    """Pipeline step: records -> grounded (and, with a mapper, tool-mapped) BKR records,
    in place. The result then carries extracted_resources as BKR records plus
    resource_grounding / resource_mapping reports; build() re-grounds when it gets the
    source again (idempotent) and trusts this pass when it does not."""
    cfg = cfg or load_config()
    records, notes = to_records(result)
    records, fixes = conform([_strip_nulls(r) for r in records], json.loads(Path(cfg["schema"]).read_text()))
    notes += fixes
    grounding = ground(records, source_text, cfg)
    grounding["warnings"] = notes + grounding["warnings"]
    grounding["grounded"] = source_text is not None
    if source_text is not None:
        grounding["source_sha256"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    for k in ("judge_resource", "aligned_resources"):
        result.pop(k, None)
    result["extracted_resources"] = [_strip_nulls(r) for r in records]
    result["task_type"] = "resource"
    result["resource_grounding"] = grounding
    if mapper is not None:
        result["resource_mapping"] = map_concepts(result["extracted_resources"], mapper, cfg)
    return result


TYPE_SYNONYMS = {"library": "software_library", "package": "software_library", "framework": "software_library",
                 "software": "tool", "toolkit": "tool", "application": "tool", "corpus": "dataset",
                 "database": "archive", "repository": "archive", "portal": "archive", "atlas": "dataset",
                 "data_model": "schema", "metadata_standard": "schema", "standard": "schema",
                 "paper": "publication", "article": "publication", "algorithm": "model"}


def conform(records: list[dict], schema: dict) -> tuple[list[dict], list[str]]:
    """Make records conform to the extraction schema by REMOVING what does not fit:
    an off-vocabulary enum value, a wrongly typed field, an unknown key. A record
    whose extracted_type cannot be normalised, or that lacks a name, is dropped. One
    bad value never costs the paper its whole resource KG, and nothing is guessed."""
    import jsonschema
    notes: list[str] = []
    kept = []
    for rec in records:
        et = str(rec.get("extracted_type") or "").strip().lower().replace(" ", "_").replace("-", "_")
        rec["extracted_type"] = TYPE_SYNONYMS.get(et, et)
        if not rec.get("name"):
            notes.append(f"{rec.get('record_id')}: record without a name dropped")
            continue
        kept.append(rec)
    validator = jsonschema.Draft202012Validator(schema)
    for _ in range(50):
        errors = list(validator.iter_errors({"extracted_resources": kept}))
        if not errors:
            break
        changed = False
        for e in sorted(errors, key=lambda e: -len(e.absolute_path)):
            path = list(e.absolute_path)
            if len(path) < 2:
                continue
            idx = path[1]
            if len(path) == 2 or (len(path) == 3 and path[2] in ("extracted_type", "name", "record_id")):
                if e.validator == "required" and "record_id" in e.message and idx < len(kept):
                    kept[idx]["record_id"] = f"r{idx + 1}"
                    changed = True
                    continue
                if e.validator != "additionalProperties":
                    if idx < len(kept):
                        notes.append(f"{kept[idx].get('name')}: record dropped — {e.message[:120]}")
                        kept[idx] = None
                        changed = True
                    continue
            parent = {"extracted_resources": kept}
            for k in path[:-1]:
                parent = parent[k] if parent is not None else None
            if parent is None:
                continue
            if e.validator == "additionalProperties" and isinstance(e.instance, dict):
                allowed = set((e.schema.get("properties") or {}))
                for extra in [k for k in e.instance if k not in allowed]:
                    e.instance.pop(extra, None)
                    notes.append(f"{'/'.join(map(str, path[2:])) or 'record'}: unknown field '{extra}' removed")
                changed = True
                continue
            leaf = path[-1]
            try:
                if isinstance(parent, list):
                    parent[leaf] = None
                    notes.append(f"{'/'.join(map(str, path[2:]))}: removed — {e.message[:120]}")
                elif isinstance(parent, dict) and leaf in parent:
                    parent.pop(leaf)
                    notes.append(f"{'/'.join(map(str, path[2:]))}: removed — {e.message[:120]}")
                changed = True
            except (KeyError, IndexError, TypeError):
                pass
        kept = [r for r in kept if r is not None]
        for r in kept:
            _drop_none_items(r)
        if not changed:
            break
    return kept, notes


def _drop_none_items(x):
    if isinstance(x, dict):
        for v in x.values():
            _drop_none_items(v)
    elif isinstance(x, list):
        x[:] = [v for v in x if v is not None]
        for v in x:
            _drop_none_items(v)


def build(result: dict, *, source_path: Optional[Path] = None, source_text: Optional[str] = None,
          mapper=None, cfg: Optional[dict] = None, alias: Optional[dict] = None,
          validate_schema: bool = True) -> tuple[str, dict]:
    """Library entry point: a resource result -> (BKR Turtle, report)."""
    cfg = cfg or load_config()
    if source_text is None and source_path is not None:
        from json_to_ttl import load_source_text
        source_text = load_source_text(Path(source_path))
    records, notes = to_records(result)
    report: dict[str, Any] = {"kind": "resource_kg", "ontology": Path(cfg["ontology"]).name,
                              "records_in": len(raw_records(result)), "records": len(records),
                              "warnings": list(notes)}
    if not records:
        report["warnings"].append("no resource records in the result")
    if source_text is None and (result.get("resource_grounding") or {}).get("grounded"):
        # grounded by the pipeline against its text; nothing to re-check here
        report["grounding"] = {"source_text": False, "removed": [], "n_removed": 0, "warnings": [],
                               "grounded_earlier": True}
    else:
        report["grounding"] = ground(records, source_text, cfg)
    report["warnings"].extend(report["grounding"]["warnings"])
    if mapper is not None:
        report["mapping"] = map_concepts(records, mapper, cfg)
    paper_id, pub_iri, doi = paper_identity(result, source_path)
    attach_provenance(records, result, paper_id, doi)
    records = [_strip_nulls(r) for r in records]
    if validate_schema and records:
        records, fixes = conform(records, json.loads(Path(cfg["schema"]).read_text()))
        report["schema_repairs"] = fixes
        report["warnings"].extend(fixes)
    _scope_ids(records, paper_id)
    documents = {paper_id: pub_iri, paper_id.lower(): pub_iri, display_id(paper_id): pub_iri}
    if doi:
        documents[doi] = documents[doi.lower()] = pub_iri
    conv = bkr_convert.Converter(
        cfg["instance_base"], bkr_convert.load_type_map(cfg["ontology"]),
        emit_skos=bool(cfg.get("emit_skos", True)),
        resource_key=(lambda rec: norm_name(rec["name"])) if cfg.get("global_resource_key", True) else None,
        documents=documents)
    PROV_SRC = URIRef("http://www.w3.org/ns/prov#hadPrimarySource")
    for rec in records:
        res = conv.convert(rec)
        record = conv.iri(rec["record_id"], "record")
        # per-paper attribution of what has no "stated in" property of its own: the
        # versions this paper states, and the resources it mentions (dcterms:references
        # on its record; stub resolution rewires the object like any other reference)
        for v in rec.get("versions") or []:
            conv.g.add((conv.iri(rec["record_id"], "version", v["version"]), PROV_SRC, URIRef(pub_iri)))
        for m in rec.get("mentions") or []:
            conv.g.add((record, DCT.references, conv.iri("mentioned", m["name"])))
        if any(rec.get(f) for f in LOOSE):  # the record-level scope (topics, tasks, modalities, species)
            conv.g.add((conv.iri(rec["record_id"], "scope", "declared-root"), BKR.scopeAssertedIn, URIRef(pub_iri)))
        for i, f in enumerate(rec.get("failure_modes") or []):
            conv.g.add((conv.iri(rec["record_id"], "failure", i), PROV_SRC, URIRef(pub_iri)))
        for i, _l in enumerate(rec.get("limitations") or []):
            conv.g.add((conv.iri(rec["record_id"], "limitation", i), PROV_SRC, URIRef(pub_iri)))
        for direction in ("inputs", "outputs"):
            for i, _x in enumerate(rec.get(direction) or []):
                conv.g.add((conv.iri(rec["record_id"], direction[:-1], i), PROV_SRC, URIRef(pub_iri)))
    g = conv.g
    _describe_publication(g, URIRef(pub_iri), result, doi, paper_id)
    merged, n_stubs, unmatched = bkr_stubs.resolve(g, alias)
    report.update({
        "paper_id": paper_id, "publication": pub_iri, "triples": len(g),
        "counts": {"resources": len(set(g.subjects(RDF.type, BKR.Resource))),
                   "records": len(set(g.subjects(RDF.type, URIRef("http://www.w3.org/ns/dcat#CatalogRecord")))),
                   "mapping_decisions": len(set(g.subjects(RDF.type, NER.ConceptMappingDecision))),
                   "skos_shortcuts": conv.skos_count, "evidence_mentions": len(set(g.subjects(RDF.type, NER.EntityMention))),
                   "stubs_merged": merged, "stubs_kept": len(unmatched)},
        "stubs_kept": unmatched})
    report["warnings"].extend(conv.warnings)
    return g.serialize(format="turtle"), report


def _describe_publication(g: rdflib.Graph, pub: URIRef, result: dict, doi: Optional[str], paper_id: str) -> None:
    """The work node, described as json_to_ttl describes it (ner:Publication), so the two
    graphs agree on the node they share."""
    meta = result.get("source_metadata") or {}
    g.add((pub, RDF.type, NER.SourceDocument))
    g.add((pub, RDF.type, OBO.IAO_0000310))
    g.add((pub, RDF.type, PROV.Entity))
    if doi or meta.get("pmid") or meta.get("journal"):
        g.add((pub, RDF.type, NER.Publication))
    g.add((pub, NER.sourceIdentifier, Literal(display_id(paper_id))))
    title = meta.get("paper_title") or meta.get("title")
    if title:
        g.add((pub, NER.title, Literal(title)))  # plain, as json_to_ttl writes it
        g.add((pub, RDFS.label, Literal(title)))
    if doi:
        g.add((pub, NER.doi, Literal(doi)))
    # what KIND of source this is, so a catalogue mixing papers and curated tables can be
    # queried by it: a DataCite general type (BKR's resource-type vocabulary) and the media type
    g.add((pub, DCT.type, URIRef(f"https://brainkb.org/resource/resource-type/{source_kind(meta, doi, paper_id)}")))
    media = _media_type(str(meta.get("source_path") or paper_id))
    if media:
        g.add((pub, BKR.mediaType, Literal(media)))
    g.bind("ner", NER)
    g.bind("kb", Namespace(str(pub).rsplit("/", 1)[0] + "/"))


TABLE_SUFFIXES = (".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".ods")
_KIND = {"spreadsheet": "Dataset", "table": "Dataset", "curated_table": "Dataset", "dataset": "Dataset",
         "publication": "JournalArticle", "paper": "JournalArticle", "article": "JournalArticle",
         "journal_article": "JournalArticle", "preprint": "Preprint", "readme": "Software",
         "model_card": "Software", "documentation": "Text", "webpage": "Text", "report": "Report"}


def source_kind(meta: dict, doi: Optional[str], paper_id: str) -> str:
    """DataCite resourceTypeGeneral of the SOURCE (not of the resources it lists):
    JournalArticle / Preprint for a paper, Dataset for a curated resource table, Text
    otherwise. source_metadata.document_type (or source_type) wins when given."""
    stated = str(meta.get("document_type") or meta.get("source_type") or "").strip().lower().replace(" ", "_")
    if stated in _KIND:
        return _KIND[stated]
    name = str(meta.get("source_path") or meta.get("source_id") or paper_id).lower()
    if name.endswith(TABLE_SUFFIXES):
        return "Dataset"
    if re.match(r"^10\.1101/", doi or ""):
        return "Preprint"
    if doi or meta.get("pmid") or meta.get("journal"):
        return "JournalArticle"
    return "Text"


def _media_type(path: str) -> Optional[str]:
    from json_to_ttl import TtlConfig
    suffix = Path(path).suffix.lower()
    return (TtlConfig().raw.get("media_types") or {}).get(suffix) if suffix else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def is_resource_graph(g: rdflib.Graph) -> bool:
    return any(str(o).startswith(str(BKR)) for o in g.objects(None, RDF.type))


def _declared(*graphs: rdflib.Graph) -> set[str]:
    out = set()
    for g in graphs:
        out.update(str(s) for s in g.subjects() if isinstance(s, URIRef))
    return out


def _components(g: rdflib.Graph, base: str, extra: set) -> int:
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def inst(n):
        return isinstance(n, BNode) or (isinstance(n, URIRef) and (str(n).startswith(base) or n in extra))

    nodes = set()
    for s, p, o in g:
        if p == RDF.type or isinstance(o, Literal):
            if inst(s):
                nodes.add(s)
            continue
        if inst(s) and inst(o):
            parent[find(s)] = find(o)
            nodes.update((s, o))
        elif inst(s):
            nodes.add(s)
    return len({find(n) for n in nodes})


def validate_graph(data: rdflib.Graph, cfg: Optional[dict] = None) -> dict:
    cfg = cfg or load_config()
    from pyshacl import validate
    onto = rdflib.Graph().parse(cfg["ontology"], format="xml")
    ner = rdflib.Graph().parse(str(NER_ONTOLOGY), format="xml")
    shapes = rdflib.Graph().parse(cfg["shapes"], format="turtle")
    declared = _declared(onto, ner)
    undeclared = Counter()
    for s, p, o in data:
        for t in ((o,) if p == RDF.type else ()) + (p,):
            ts = str(t)
            if (ts.startswith(str(BKR)) or ts.startswith(str(NER))) and ts not in declared:
                # vocabulary values (…/evidence-level/x, …/mapping-status/x) are individuals
                if re.search(r"/[a-z]+(-[a-z]+)+/[^/]+$", ts):
                    continue
                undeclared[ts] += 1
    conforms, rg, _text = validate(data + onto, shacl_graph=shapes, inference="none", advanced=True)
    silence = set(cfg.get("source_silence_shapes") or [])
    absence = _clean(cfg.get("absence_shapes"))
    DCAT_REC = URIRef("http://www.w3.org/ns/dcat#CatalogRecord")

    def declared_absent(focus, field):
        node = URIRef(focus)
        if any(str(k).startswith("mentioned/") for k in data.objects(node, DCT.identifier)):
            return True  # a stub: named by the source, never described by it
        return any(str(f).split(".")[0].split("[")[0] == field
                   for rec in data.subjects(BKR.describesResource, node)
                   for f in data.objects(rec, BKR.notFoundField))
    violations, findings, warnings = [], [], Counter()
    for r in rg.subjects(RDF.type, SH.ValidationResult):
        sev = str(rg.value(r, SH.resultSeverity)).rsplit("#", 1)[-1]
        shape = rg.value(r, SH.sourceShape)
        shape_name = str(shape).rsplit("/", 1)[-1] if isinstance(shape, URIRef) else None
        if shape_name is None:  # a property shape: name it by its node shape
            parent = next(iter(shapes.subjects(SH.property, shape)), None)
            shape_name = str(parent).rsplit("/", 1)[-1] if parent is not None else "property-shape"
        msg = str(rg.value(r, SH.resultMessage) or "")
        row = {"shape": shape_name, "focus": str(rg.value(r, SH.focusNode)), "message": msg[:240]}
        if sev != "Violation":
            warnings[f"{shape_name}: {msg[:120]}"] += 1
        elif shape_name in silence or (shape_name in absence and declared_absent(row["focus"], absence[shape_name])):
            findings.append(row)
        else:
            violations.append(row)
    base = cfg["instance_base"]
    pubs = {s for s in data.subjects(RDF.type, NER.SourceDocument)}
    n_comp = _components(data, base, pubs)
    problems = []
    if undeclared:
        problems.append(f"{len(undeclared)} undeclared bkr:/ner: term(s): " + ", ".join(sorted(undeclared)[:8]))
    if n_comp > 1:
        problems.append(f"{n_comp} connected components (expected 1: an island lost its provenance)")
    return {"ok": not violations and not problems, "triples": len(data),
            "violations": violations, "source_silence_findings": findings,
            "warnings": [{"message": k, "count": v} for k, v in warnings.most_common()],
            "undeclared_terms": dict(undeclared), "components": n_comp, "problems": problems,
            "counts": {"violations": len(violations), "source_silence_findings": len(findings),
                       "warnings": sum(warnings.values())}}


def validate_file(path: Path, cfg: Optional[dict] = None) -> dict:
    g = rdflib.Graph().parse(str(path), format="turtle")
    rep = validate_graph(g, cfg)
    rep["file"] = str(path)
    return rep


def print_validation(rep: dict, out=sys.stderr) -> None:
    c = rep["counts"]
    print(f"{rep.get('file', 'graph')}: {rep['triples']} triples — {'OK' if rep['ok'] else 'FAIL'}; "
          f"{c['violations']} violation(s), {c['source_silence_findings']} source-silence finding(s), "
          f"{c['warnings']} warning(s), {rep['components']} component(s)", file=out)
    for p in rep["problems"]:
        print(f"  problem: {p}", file=out)
    for v in rep["violations"][:20]:
        print(f"  violation [{v['shape']}] {v['focus']}: {v['message']}", file=out)
    if rep["source_silence_findings"]:
        shapes = Counter(f["shape"] for f in rep["source_silence_findings"])
        print("  findings about the source (not graph defects; never invent a licence): "
              + ", ".join(f"{k} x{v}" for k, v in shapes.items()), file=out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_out(result_path: Path) -> Path:
    from json_to_ttl import default_ttl_path
    return default_ttl_path(result_path)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="resource result JSON -> BKR Turtle")
    b.add_argument("result", type=Path)
    b.add_argument("--source", type=Path, help="the source document (grounding needs its text)")
    b.add_argument("--out", type=Path)
    b.add_argument("--report", type=Path)
    b.add_argument("--map", action="store_true",
                   help="map scope-dimension labels with the ConceptMapper (concept_mapping.json cascade)")
    b.add_argument("--sources", help="mapper sources, e.g. trusted,bioportal (default concept_mapping.json)")
    b.add_argument("--alias", type=Path, help="JSON {mention name: resource name} for stub variants")
    b.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    v = sub.add_parser("validate", help="gate a BKR Turtle file (SHACL + vocabulary + connectivity)")
    v.add_argument("ttl", type=Path, nargs="+")
    v.add_argument("--json", type=Path)
    v.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "validate":
        reps, rc = [], 0
        for p in args.ttl:
            try:
                rep = validate_file(p, cfg)
            except Exception as e:  # parse error
                print(f"{p}: cannot parse: {e}", file=sys.stderr)
                return 2
            print_validation(rep)
            reps.append(rep)
            rc |= 0 if rep["ok"] else 1
        if args.json:
            args.json.write_text(json.dumps(reps if len(reps) > 1 else reps[0], indent=2) + "\n")
        return rc
    result = json.loads(args.result.read_text())
    mapper = None
    if args.map:
        from concept_mapping import ConceptMapper
        mapper = ConceptMapper(sources=args.sources.split(",") if args.sources else None)
        if not mapper.usable_sources():
            print("concept mapping requested but no source is usable (index the trusted ontologies, "
                  "start the local mapper or set BIOPORTAL_API_KEY)", file=sys.stderr)
            return 1
    alias = json.loads(args.alias.read_text()) if args.alias else None
    ttl, report = build(result, source_path=args.source, mapper=mapper, cfg=cfg, alias=alias)
    out = args.out or _default_out(args.result)
    out.write_text(ttl)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    _print_build(out, report)
    return 0


def _print_build(out: Path, report: dict) -> None:
    c = report["counts"]
    print(f"wrote {out}: {report['triples']} triples, {c['resources']} resources, {c['records']} records, "
          f"{c['mapping_decisions']} mapping decisions ({c['skos_shortcuts']} SKOS shortcuts), "
          f"{c['evidence_mentions']} evidence quotes, {c['stubs_merged']} mention stubs merged, "
          f"{c['stubs_kept']} kept; grounding removed {report['grounding']['n_removed']} unsupported value(s)",
          file=sys.stderr)
    for w in report["warnings"][:20]:
        print(f"  warning: {w}", file=sys.stderr)
    if len(report["warnings"]) > 20:
        print(f"  ... and {len(report['warnings']) - 20} more warnings", file=sys.stderr)
    print(f"next: python -m scripts.resource_kg validate {out}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(_main())
