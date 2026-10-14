"""The correction layer shared by the judge stage and the human-feedback loop.

Every mode goes through the same four stages (references/review-loop.md):

    extract -> concept map -> judge (+ combiner) -> human feedback (optional) -> output
                                                       ^                          |
                                                       +---- optional output loop +

Judges and humans correct through ONE vocabulary of operations, applied here and
nowhere else, so both are held to the same rules:

    drop     remove an item (NER mentions; ABCD -> rejected[]; AIT -> quarantined).
             Every drop keeps the full record, so a later `restore` can undo it.
    restore  bring back an item an earlier round dropped (never a new item).
    set      change one field to a value from that field's allowed vocabulary.
    remap    re-run the mapping TOOL with a better query; the tool decides the id.
             A human may also pin an id on an AIT mapping (mapped_by=human).
    demote   remove a mapping (the item stays, unmapped).
    approve  mark an item human-verified.
    note     attach a remark.

What makes this a correction layer and not an edit box: field allowlists per mode and
item kind (FIXABLE), ids only ever from a tool (concept_mapping.ConceptMapper — the
trusted ontology files first — the ABCD dictionary, the Cognitive Atlas), and an
append-only log (`review_loop` block) with the actor, the value before and after,
and why. Nothing is added that no source stated: a reviewer who wants a new item
re-runs extraction.

    python -m scripts.review_loop map  --mode ait out/                 # concept map stage
    python -m scripts.review_loop ops  --mode abcd paper_abcd.json --ops ops.json
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = _SCRIPTS_DIR.parent
for _p in (str(_SCRIPTS_DIR), str(SKILL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MODES = ("ner", "abcd", "ait")
ACTIONS = ("drop", "restore", "set", "remap", "demote", "approve", "note")
TIERS = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch")
ACTORS = ("judge", "combiner", "human")

ROLES = ["predictor", "outcome", "mediator", "moderator", "covariate", "confounder",
         "control", "instrument", "unspecified"]
DIRECTIONS = ["positive", "negative", "null", "mixed", "unspecified"]

# field -> allowed values (a list), or a validator taking (value, record) -> error|None.
# A judge may only `set` these; so may a human. Everything else is not editable here.
FIXABLE: dict[str, dict[str, dict[str, Any]]] = {
    "ner": {"entity": {"label": "nonempty", "tier": TIERS},
            "key_term": {"label": "nonempty", "tier": TIERS}},
    "abcd": {"variable": {"role": ROLES, "respondent": ["parent", "youth", "teacher", "self"],
                          "timepoint": "in_quote"},
             "construct": {"construct_kind": ["concept", "task"], "tier": TIERS},
             "model": {"kind": ["descriptive", "correlational", "regression", "mixed_model", "mediation",
                                "moderation", "moderated_mediation", "growth_curve", "sem", "survival",
                                "classification", "machine_learning", "other"]},
             "finding": {"direction": DIRECTIONS, "effect_size": "in_quote", "statistic": "in_quote",
                         "subgroup": "in_quote"}},
    "ait": {"entity": {"entity_type": ["cell_type", "gene", "species", "brain_region"],
                       "hierarchy_level": ["class", "subclass", "supertype", "cluster", "type", "unspecified"],
                       "species_role": ["experimental", "antibody_host", "citation_only", "reference_dataset"],
                       "assertion_type": ["asserted", "inferred"]},
            "mapping": {"skos_relation": ["skos:" + t for t in TIERS]},
            "concept": {"tier": TIERS}},
}

ABCD_SECTIONS = {"variables": "variable", "constructs": "construct", "models": "model", "findings": "finding"}
# extractor label used to route an AIT entity / ABCD construct through the trusted
# ontologies (concept_mapping.json label_routing)
AIT_LABEL = {"cell_type": "CellType", "gene": "Gene", "species": "Species", "brain_region": "BrainRegion"}
CONSTRUCT_LABEL = "CognitiveConstruct"
CONCEPT_TABLE = "concept_mappings.csv"
CONCEPT_COLUMNS = ["paper_id", "concept_id", "mention_id", "entity_type", "query", "ontology_id",
                   "ontology_label", "ontology", "mapping_source", "match_tier",
                   "concept_mapping_provenance", "mapped_by", "notes"]
LOG_FILE = "review_loop.json"


class OpError(ValueError):
    pass


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rid(prefix: str, *parts: Any) -> str:
    """Content id, stable across re-runs of the same deterministic stages."""
    h = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}:{h}"


# --------------------------------------------------------------------------- #
# record views: what a judge or a human sees, keyed by a stable id
# --------------------------------------------------------------------------- #

def _ev(item: dict) -> dict:
    return item.get("evidence") if isinstance(item.get("evidence"), dict) else {}


def abcd_records(doc: dict) -> list[dict]:
    out = []
    for section, kind in ABCD_SECTIONS.items():
        for item in doc.get(section) or []:
            if not isinstance(item, dict):
                continue
            ev = _ev(item)
            surface = {"variable": item.get("mention_as_written") or item.get("name"),
                       "construct": item.get("construct"),
                       "model": item.get("specification") or item.get("model_id"),
                       "finding": item.get("statement")}[kind]
            iid = item.get("review_id") or rid(kind[:3], surface, ev.get("start"), ev.get("quote", "")[:80])
            item["review_id"] = iid
            mapping = None
            if kind == "variable" and item.get("dictionary_match"):
                dm = item["dictionary_match"] if isinstance(item["dictionary_match"], dict) else {}
                mapping = {"kind": "dictionary", "id": item.get("name"), "label": dm.get("label") or item.get("label"),
                           "table": item.get("nda_or_nbdc_table"), "status": item.get("dictionary_status")}
            elif kind == "construct" and (item.get("construct_id") or item.get("ontology_id")):
                mapping = {"kind": "ontology", "id": item.get("ontology_id") or item.get("construct_id"),
                           "label": item.get("ontology_label") or item.get("construct_label"),
                           "source": item.get("ontology_mapping_source") or item.get("mapping_source"),
                           "atlas_id": item.get("construct_id"), "tier": item.get("ontology_match_tier")}
            out.append({"id": iid, "mode": "abcd", "kind": kind, "section": section, "surface": surface,
                        "fields": {f: item.get(f) for f in FIXABLE["abcd"][kind] if f != "tier"},
                        "mapping": mapping,
                        "evidence": {"quote": ev.get("quote"), "anchor_method": ev.get("anchor_method"),
                                     "section": ev.get("section"), "occurrences": ev.get("occurrences_in_paper")},
                        "ref": item})
    return out


def _ait():
    from scripts import ait_tables
    return ait_tables


def ait_records(data: dict) -> list[dict]:
    at = _ait()
    out = []
    for e in data.get("entities.csv") or []:
        out.append({"id": e.get("mention_id"), "mode": "ait", "kind": "entity", "surface": e.get("label_verbatim"),
                    "fields": {f: e.get(f) for f in FIXABLE["ait"]["entity"]},
                    "evidence": {"sentences": at.split_multi(e.get("evidence_sentence", "")),
                                 "verified": at.split_multi(e.get("evidence_verified", ""))},
                    "mapping": None, "ref": e})
    ents = {e.get("mention_id"): e for e in data.get("entities.csv") or []}
    for m in at.current_mappings(data.get("mappings.csv") or []):
        # an edge with no node (skos_relation none) is still an item: a curator may
        # re-point it; judges only see edges that have a node (mapping is None here)
        e = ents.get(m.get("mention_id")) or {}
        out.append({"id": m.get("mapping_id"), "mode": "ait", "kind": "mapping",
                    "surface": m.get("cell_type_name_as_in_paper"),
                    "fields": {"skos_relation": m.get("skos_relation")},
                    "mapping": None if not m.get("ait_node_id") else
                    {"kind": "ait", "id": m.get("ait_node_id"), "label": m.get("ait_cell_type_label"),
                     "taxonomy": m.get("ait_taxonomy_used"), "level": m.get("ait_hierarchy_level"),
                     "basis": m.get("basis_for_match"), "evidence": m.get("mapping_evidence")},
                    "evidence": {"sentences": at.split_multi(e.get("evidence_sentence", ""))[:3],
                                 "marker_genes": e.get("marker_genes"), "brain_region": e.get("brain_region"),
                                 "species": e.get("species")},
                    "ref": m})
    for c in data.get(CONCEPT_TABLE) or []:
        mapped = c.get("concept_mapping_provenance") == "tool" and c.get("ontology_id")
        e = ents.get(c.get("mention_id")) or {}
        out.append({"id": c.get("concept_id"), "mode": "ait", "kind": "concept", "surface": c.get("query"),
                    "fields": {"tier": c.get("match_tier")},
                    "mapping": None if not mapped else
                    {"kind": "ontology", "id": c.get("ontology_id"), "label": c.get("ontology_label"),
                     "source": c.get("mapping_source"), "tier": c.get("match_tier")},
                    "evidence": {"sentences": at.split_multi(e.get("evidence_sentence", ""))[:3],
                                 "entity_type": c.get("entity_type")},
                    "ref": c})
    return out


def ner_records(result: dict) -> list[dict]:
    from group_by_entity import mention_groups
    out = []
    for g in mention_groups(result):
        it = g["items"][0]
        mapped = it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id")
        out.append({"id": g["id"], "mode": "ner", "kind": g["kind"], "surface": g["surface"],
                    "fields": {"label": g["label"]},
                    "mapping": {"kind": "ontology", "id": it.get("ontology_id"), "label": it.get("ontology_label"),
                                "source": it.get("mapping_source"), "tier": it.get("mapping_tier")} if mapped else None,
                    "evidence": {"sentences": list(dict.fromkeys(i.get("sentence") for i in g["items"]
                                                                 if i.get("sentence")))[:3],
                                 "mentions": len(g["items"])},
                    "judge_score": it.get("judge_score"), "ref": g})
    return out


def records(mode: str, data: dict) -> list[dict]:
    return {"ner": ner_records, "abcd": abcd_records, "ait": ait_records}[mode](data)


# --------------------------------------------------------------------------- #
# the tools a remap may use
# --------------------------------------------------------------------------- #

class Tools:
    """Lazily-built mapping tools. ids come only from here, never from a judge or a
    prompt. A driver that already holds the dictionary/atlas passes them in."""

    def __init__(self, *, mapper=None, dictionary=None, context_index=None, atlas=None,
                 gate_kwargs: Optional[dict] = None, offline: bool = False):
        self._mapper = mapper
        self.dictionary = dictionary
        self.context_index = context_index
        self.atlas = atlas
        self.gate_kwargs = gate_kwargs or {}
        self.offline = offline

    @property
    def mapper(self):
        if self._mapper is None:
            from concept_mapping import ConceptMapper
            sources = ["trusted"] if self.offline else None
            self._mapper = ConceptMapper(sources=sources)
        return self._mapper

    def lookup(self, query: str, label: Optional[str]) -> dict:
        probe = {"term": query, "label": label}
        self.mapper.map_items([probe], "term")
        return probe


def _tool_fields(hit: dict) -> dict:
    return {k: hit.get(k) for k in ("ontology_id", "ontology_label", "ontology", "mapping_source",
                                     "match_tier", "ontology_match_type", "alignment_method")}


# --------------------------------------------------------------------------- #
# concept map stage for record modes: trusted ontologies first, then the cascade
# --------------------------------------------------------------------------- #

def map_abcd(doc: dict, tools: Tools) -> dict:
    """Constructs (and findings' constructs) through ConceptMapper, next to the
    Cognitive Atlas id the verifier already attached. The trusted files are first."""
    probes = []
    for section in ("constructs", "findings"):
        for item in doc.get(section) or []:
            term = (item.get("construct") or "").strip() if isinstance(item, dict) else ""
            if term and not (item.get("ontology_mapping_provenance") == "tool" and item.get("ontology_id")):
                probes.append((item, {"term": term, "label": CONSTRUCT_LABEL}))
    if probes:
        tools.mapper.map_items([p for _, p in probes], "term")
    mapped = 0
    for item, p in probes:
        if p.get("concept_mapping_provenance") == "tool" and p.get("ontology_id"):
            item.update({"ontology_id": p["ontology_id"], "ontology_label": p.get("ontology_label"),
                         "ontology": p.get("ontology"), "ontology_mapping_source": p.get("mapping_source"),
                         "ontology_match_tier": p.get("match_tier") or "exactMatch",
                         "ontology_mapping_provenance": "tool"})
            mapped += 1
        else:
            item["ontology_mapping_provenance"] = "unmapped"
            item["ontology_mapping_sources_tried"] = p.get("mapping_sources_tried") or list(tools.mapper.sources)
    meta = tools.mapper.meta()
    doc.setdefault("provenance", {})["concept_mapping"] = {
        "label_route": CONSTRUCT_LABEL, "items": len(probes), "mapped": mapped,
        "sources_priority": meta.get("sources_priority"), "mapped_by_source": meta.get("mapped_by_source"),
        "trusted_ontologies": [t["name"] for t in meta.get("trusted_ontologies") or []], "at": utc_now()}
    return doc


def map_ait(data: dict, tools: Tools) -> dict:
    """entities.csv -> concept_mappings.csv (one row per entity, mapped or not)."""
    at = _ait()
    rows, probes = [], []
    old = {c.get("mention_id"): c for c in data.get(CONCEPT_TABLE) or []}
    for e in data.get("entities.csv") or []:
        label = AIT_LABEL.get(e.get("entity_type", ""))
        if not label or at.is_quarantined(e):
            continue
        prev = old.get(e.get("mention_id"))
        if prev and prev.get("mapped_by") == "human":
            rows.append(prev)  # a curator's decision is not re-derived
            continue
        q = e.get("label_normalized") or e.get("label_verbatim") or ""
        probes.append((e, {"term": q, "label": label}))
    if probes:
        tools.mapper.map_items([p for _, p in probes], "term")
    for e, p in probes:
        tool = p.get("concept_mapping_provenance") == "tool" and p.get("ontology_id")
        rows.append({"paper_id": e.get("paper_id"), "concept_id": f"{e.get('mention_id')}:C",
                     "mention_id": e.get("mention_id"), "entity_type": e.get("entity_type"), "query": p["term"],
                     "ontology_id": p.get("ontology_id") or "", "ontology_label": p.get("ontology_label") or "",
                     "ontology": p.get("ontology") or "", "mapping_source": p.get("mapping_source") or "",
                     "match_tier": (p.get("match_tier") or "exactMatch") if tool else "",
                     "concept_mapping_provenance": "tool" if tool else "unmapped", "mapped_by": "code",
                     "notes": "" if tool else "tried " + ",".join(p.get("mapping_sources_tried") or [])})
        iri = p.get("ontology_id") or ""
        if tool and e.get("entity_type") == "species" and "NCBITaxon_" in iri and not e.get("ncbi_taxon_id"):
            e["ncbi_taxon_id"] = iri.rsplit("_", 1)[1]
            e["field_provenance"] = at.add_provenance(e.get("field_provenance", ""), "ncbi_taxon_id",
                                                      "code:ontology_lookup")
    data[CONCEPT_TABLE] = rows
    return data


def map_records(mode: str, data: dict, tools: Tools) -> dict:
    if mode == "abcd":
        return map_abcd(data, tools)
    if mode == "ait":
        return map_ait(data, tools)
    raise OpError("NER concept mapping is concept_mapping.map_result (pipeline stage 2)")


# --------------------------------------------------------------------------- #
# applying operations
# --------------------------------------------------------------------------- #

def _check_value(mode: str, kind: str, field: str, value: Any, rec: dict) -> Optional[str]:
    allowed = (FIXABLE.get(mode) or {}).get(kind, {}).get(field)
    if allowed is None:
        return f"field {field!r} is not correctable on a {mode} {kind}"
    if isinstance(allowed, (list, tuple)):
        return None if value in allowed else f"{value!r} not in {list(allowed)}"
    if allowed == "nonempty":
        return None if isinstance(value, str) and value.strip() else "empty value"
    if allowed == "in_quote":
        quote = (rec.get("evidence") or {}).get("quote") or ""
        return None if value is None or str(value) in quote else \
            f"{value!r} does not occur in the item's evidence quote (values come from the paper)"
    return "unknown validator"


class Applier:
    def __init__(self, mode: str, data: dict, *, actor: str, by: Optional[str] = None,
                 tools: Optional[Tools] = None, log: Optional[dict] = None):
        if mode not in MODES:
            raise OpError(f"mode must be one of {MODES}")
        if actor not in ACTORS:
            raise OpError(f"actor must be one of {ACTORS}")
        self.mode, self.data, self.actor, self.by = mode, data, actor, by
        self.tools = tools or Tools()
        self.log = log if log is not None else data.setdefault("review_loop", {"rounds": [], "dropped": {}})
        self.log.setdefault("rounds", [])
        self.log.setdefault("dropped", {})
        self._index()

    def _index(self) -> None:
        self.recs = {str(r["id"]).lower(): r for r in records(self.mode, self.data)}
        self.alias: dict[str, str] = getattr(self, "alias", {})

    @staticmethod
    def _anchor(rec: dict) -> int:
        """The underlying object a record views (NER: its first mention)."""
        ref = rec["ref"]
        return id(ref["items"][0]) if isinstance(ref, dict) and "items" in ref and ref["items"] else id(ref)

    def _follow(self, rec: Optional[dict]) -> None:
        """A relabel changes a NER item's id ('BDNF|Gene' -> 'BDNF|Protein'): later ops
        in the same round may still use the old id, so it is aliased to the new one."""
        if rec is None:
            return
        anchor = self._anchor(rec)
        new = next((k for k, r in self.recs.items() if self._anchor(r) == anchor), None)
        if new and new != str(rec["id"]).lower():
            self.alias[str(rec["id"]).lower()] = new

    # -- per-mode primitives ------------------------------------------------
    def _drop(self, rec: dict, reason: str) -> dict:
        ref = rec["ref"]
        if self.mode == "ner":
            ids = {id(it) for it in ref["items"]}
            key = "entities" if ref["kind"] == "entity" else "key_terms"
            self.data[key] = [it for it in self.data.get(key) or [] if id(it) not in ids]
            self.log["dropped"][rec["id"]] = {"mode": "ner", "key": key, "items": copy.deepcopy(ref["items"])}
        elif self.mode == "abcd":
            sec = rec["section"]
            self.data[sec] = [it for it in self.data.get(sec) or [] if it is not ref]
            self.data.setdefault("rejected", []).append({
                "section": sec, "reason": f"{self.actor}:{reason}"[:400], "claim": copy.deepcopy(ref),
                "claimed_quote": (_ev(ref) or {}).get("quote"), "review_id": rec["id"]})
            self.log["dropped"][rec["id"]] = {"mode": "abcd", "section": sec, "item": copy.deepcopy(ref)}
            v = self.data.setdefault("verification", {})
            v["rejected_total"] = int(v.get("rejected_total") or 0) + 1
            v[f"rejected_by_{self.actor}"] = int(v.get(f"rejected_by_{self.actor}") or 0) + 1
        else:
            at = _ait()
            if rec["kind"] == "entity":
                before = copy.deepcopy(ref)
                ref["extraction_flag"] = "unverified_evidence"
                ref["field_provenance"] = at.add_provenance(ref.get("field_provenance", ""), "extraction_flag",
                                                            self._prov())
                for m in [m for m in at.current_mappings(self.data.get("mappings.csv") or [])
                          if m.get("mention_id") == ref.get("mention_id") and m.get("ait_node_id")]:
                    self._supersede(m, none_reason=f"entity quarantined by {self.actor}: {reason}")
                self.data[CONCEPT_TABLE] = [c for c in self.data.get(CONCEPT_TABLE) or []
                                            if c.get("mention_id") != ref.get("mention_id")]
                self.log["dropped"][rec["id"]] = {"mode": "ait", "kind": "entity", "row": before}
            elif rec["kind"] == "mapping":
                self.log["dropped"][rec["id"]] = {"mode": "ait", "kind": "mapping", "row": copy.deepcopy(ref)}
                self._supersede(ref, none_reason=f"{self.actor}: {reason}")
            else:
                self._demote(rec, reason)
        return {"dropped": True}

    def _restore(self, iid: str) -> dict:
        rec = self.log["dropped"].get(iid) or next((v for k, v in self.log["dropped"].items()
                                                    if k.lower() == iid.lower()), None)
        if rec is None:
            raise OpError(f"{iid!r} was not dropped by an earlier round (restore never adds new items)")
        if rec["mode"] == "ner":
            self.data.setdefault(rec["key"], []).extend(copy.deepcopy(rec["items"]))
        elif rec["mode"] == "abcd":
            self.data.setdefault(rec["section"], []).append(copy.deepcopy(rec["item"]))
            self.data["rejected"] = [r for r in self.data.get("rejected") or [] if r.get("review_id") != iid]
            v = self.data.setdefault("verification", {})
            v["rejected_total"] = max(0, int(v.get("rejected_total") or 0) - 1)
            v["restored_by_human"] = int(v.get("restored_by_human") or 0) + 1
        elif rec.get("kind") == "entity":
            for e in self.data.get("entities.csv") or []:
                if e.get("mention_id") == rec["row"].get("mention_id"):
                    e.update(rec["row"])
            # its edges stay superseded: re-map explicitly (remap) if they should return
        else:
            row = dict(rec["row"])
            sup = self._new_mapping_id(row)
            row.update({"supersedes_mapping_id": self._current_head(row["mapping_id"]), "mapping_id": sup,
                        "mapped_by": "human", "notes": (row.get("notes", "") + " | restored by human").strip(" |")})
            self.data.setdefault("mappings.csv", []).append(row)
        del self.log["dropped"][next(k for k in self.log["dropped"] if k.lower() == iid.lower())]
        return {"restored": True}

    def _set(self, rec: dict, field: str, value: Any) -> dict:
        err = _check_value(self.mode, rec["kind"], field, value, rec)
        if err:
            raise OpError(err)
        ref = rec["ref"]
        if self.mode == "ner":
            before = rec["fields"].get(field) if field == "label" else rec["ref"]["items"][0].get("mapping_tier")
            for it in ref["items"]:
                if field == "label":
                    it.setdefault("label_before_review", it.get("label"))
                    it["label"] = value
                elif it.get("concept_mapping_provenance") == "tool":
                    it["mapping_tier"] = value
            return {"from": before, "to": value}
        if self.mode == "abcd":
            key = "ontology_match_tier" if field == "tier" else field
            before = ref.get(key)
            ref[key] = value
            ref.setdefault("review_changes", []).append({"field": key, "from": before, "to": value,
                                                         "by": self.actor, "at": utc_now()})
            return {"from": before, "to": value}
        at = _ait()
        if rec["kind"] == "mapping":
            before = ref.get("skos_relation")
            new = self._supersede(ref, skos=value)
            return {"from": before, "to": value, "new_mapping_id": new["mapping_id"]}
        key = "match_tier" if field == "tier" else field
        before = ref.get(key)
        ref[key] = value
        if rec["kind"] == "concept":
            ref["mapped_by"] = "human" if self.actor == "human" else ref.get("mapped_by", "code")
        else:
            ref["field_provenance"] = at.add_provenance(ref.get("field_provenance", ""), key, self._prov())
        return {"from": before, "to": value}

    def _demote(self, rec: dict, reason: str) -> dict:
        ref = rec["ref"]
        if self.mode == "ner":
            removed = sorted({str(it.get("ontology_id")) for it in ref["items"] if it.get("ontology_id")})
            for it in ref["items"]:
                it["ontology_id"] = it["ontology_label"] = it["ontology"] = None
                it["concept_mapping_provenance"] = "unmapped"
                it["alignment_method"] = f"{self.actor}_demoted"
                it.pop("mapping_tier", None)
            return {"from": removed}
        if self.mode == "abcd":
            if rec["kind"] == "variable":
                before = {"name": ref.get("name"), "table": ref.get("nda_or_nbdc_table"),
                          "status": ref.get("dictionary_status")}
                ref["demoted_mapping"] = {**before, "by": self.actor, "reason": reason}
                ref["dictionary_status"] = "unverified_variable"
                ref["nda_or_nbdc_table"] = None
                return {"from": before}
            before = {"construct_id": ref.get("construct_id"), "ontology_id": ref.get("ontology_id")}
            ref["demoted_claim"] = {**before, "by": self.actor, "reason": reason}
            ref["construct_id"] = ref["construct_label"] = None
            ref["ontology_id"] = ref["ontology_label"] = ref["ontology"] = None
            ref["mapping_provenance"] = "unmapped"
            ref["ontology_mapping_provenance"] = "unmapped"
            return {"from": before}
        if rec["kind"] == "mapping":
            new = self._supersede(ref, none_reason=f"{self.actor}: {reason}")
            return {"from": ref.get("ait_node_id"), "new_mapping_id": new["mapping_id"]}
        before = ref.get("ontology_id")
        ref.update({"ontology_id": "", "ontology_label": "", "ontology": "", "match_tier": "",
                    "concept_mapping_provenance": "unmapped",
                    "notes": f"demoted by {self.actor}: {reason}"[:300]})
        return {"from": before}

    def _remap(self, rec: dict, value: Any) -> dict:
        ref = rec["ref"]
        if self.mode == "ait" and rec["kind"] == "mapping":
            if self.actor != "human" or not isinstance(value, dict):
                raise OpError("an AIT edge can only be re-pointed by a human, with "
                              "{ait_node_id, ait_cell_type_label, ait_id?, skos_relation}: there is no local AIT "
                              "node index for a tool lookup")
            for k in ("ait_node_id", "ait_cell_type_label", "skos_relation"):
                if not value.get(k):
                    raise OpError(f"remap value needs {k}")
            if value["skos_relation"] not in FIXABLE["ait"]["mapping"]["skos_relation"]:
                raise OpError(f"skos_relation {value['skos_relation']!r} not allowed")
            new = self._supersede(ref, fields={k: value[k] for k in ("ait_node_id", "ait_cell_type_label", "ait_id",
                                                                      "ait_hierarchy_level", "skos_relation", "cl_id")
                                               if value.get(k)})
            return {"from": ref.get("ait_node_id"), "to": value["ait_node_id"], "new_mapping_id": new["mapping_id"]}
        query = value if isinstance(value, str) else (value or {}).get("query")
        if not query or not str(query).strip():
            raise OpError("remap needs a query string: the tool decides the id")
        query = str(query).strip()
        if self.mode == "abcd" and rec["kind"] == "variable":
            if self.tools.dictionary is None:
                raise OpError("re-gating a variable needs the ABCD dictionary (run through abcd_extract)")
            from scripts.abcd_verify import gate_variable
            probe = dict(ref)
            probe["name"] = query
            gated = gate_variable(probe, self.tools.dictionary, context_index=self.tools.context_index,
                                  **self.tools.gate_kwargs)
            from scripts.abcd_verify import MAPPED_STATUSES
            if gated.get("dictionary_status") not in MAPPED_STATUSES:
                raise OpError(f"the dictionary does not resolve {query!r} ({gated.get('dictionary_status')})")
            before = ref.get("name")
            keep = {k: ref[k] for k in ("mention_as_written", "evidence", "review_id") if k in ref}
            ref.clear()
            ref.update(gated)
            ref.update(keep)
            ref["remapped_by"] = {"actor": self.actor, "query": query, "from": before, "at": utc_now()}
            return {"from": before, "to": ref.get("name")}
        label = {"ner": None, "abcd": CONSTRUCT_LABEL}.get(self.mode)
        if self.mode == "ner":
            label = rec["fields"].get("label")
        elif self.mode == "ait":
            label = AIT_LABEL.get((rec.get("evidence") or {}).get("entity_type") or ref.get("entity_type", ""))
        if self.mode == "abcd" and rec["kind"] == "construct" and self.tools.atlas is not None:
            hit = self.tools.atlas.map_term(query)
            if hit:
                before = ref.get("construct_id")
                ref.update(hit)
                ref["remapped_by"] = {"actor": self.actor, "query": query, "from": before, "at": utc_now()}
                return {"from": before, "to": hit.get("construct_id"), "source": "cognitive_atlas"}
        hit = self.tools.lookup(query, label)
        if hit.get("concept_mapping_provenance") != "tool" or not hit.get("ontology_id"):
            raise OpError(f"no tool mapping for {query!r} (sources tried: "
                          f"{hit.get('mapping_sources_tried') or self.tools.mapper.sources})")
        f = _tool_fields(hit)
        if self.mode == "ner":
            before = sorted({str(it.get("ontology_id")) for it in ref["items"] if it.get("ontology_id")})
            for it in ref["items"]:
                it.update({k: v for k, v in f.items() if v is not None})
                it["concept_mapping_provenance"] = "tool"
                it["mapping_query"] = query
                it["alignment_method"] = f"{self.actor}_remapped"
                it.pop("mapping_tier", None)  # a new mapping has not been judged yet
            return {"from": before, "to": f["ontology_id"], "source": f["mapping_source"]}
        if self.mode == "abcd":
            before = ref.get("ontology_id")
            ref.update({"ontology_id": f["ontology_id"], "ontology_label": f["ontology_label"],
                        "ontology": f["ontology"], "ontology_mapping_source": f["mapping_source"],
                        "ontology_match_tier": f.get("match_tier") or "exactMatch",
                        "ontology_mapping_provenance": "tool",
                        "remapped_by": {"actor": self.actor, "query": query, "from": before, "at": utc_now()}})
            return {"from": before, "to": f["ontology_id"], "source": f["mapping_source"]}
        before = ref.get("ontology_id")
        ref.update({"query": query, "ontology_id": f["ontology_id"], "ontology_label": f["ontology_label"] or "",
                    "ontology": f["ontology"] or "", "mapping_source": f["mapping_source"] or "",
                    "match_tier": f.get("match_tier") or "exactMatch", "concept_mapping_provenance": "tool",
                    "mapped_by": "human" if self.actor == "human" else "code",
                    "notes": f"remapped by {self.actor} with query {query!r}"})
        return {"from": before, "to": f["ontology_id"], "source": f["mapping_source"]}

    def _approve(self, rec: dict) -> dict:
        ref = rec["ref"]
        stamp = {"by": self.by or "human", "at": utc_now()}
        if self.mode == "ner":
            for it in ref["items"]:
                it["human_verified"] = stamp
        elif self.mode == "abcd":
            ref["human_verified"] = stamp
        elif rec["kind"] == "entity":
            ref["field_provenance"] = _ait().add_provenance(ref.get("field_provenance", ""), "label_verbatim",
                                                            "human:curator")
        else:
            ref["mapped_by"] = "human"
        return {"approved": True}

    def _note(self, rec: dict, text: str) -> dict:
        ref = rec["ref"]
        if self.mode == "ner":
            for it in ref["items"]:
                it["review_notes"] = (it.get("review_notes") or []) + [text]
        elif self.mode == "abcd":
            ref["review_notes"] = (ref.get("review_notes") or []) + [text]
        else:
            ref["notes"] = (ref.get("notes", "") + f" | {self.actor}: {text}").strip(" |")[:600]
        return {"noted": True}

    # -- AIT helpers ----------------------------------------------------------
    def _prov(self) -> str:
        return "human:curator" if self.actor == "human" else "llm:review"

    def _new_mapping_id(self, row: dict) -> str:
        ids = {m.get("mapping_id") for m in self.data.get("mappings.csv") or []}
        n, base = 1, row.get("mapping_id") or f"{row.get('mention_id')}:X"
        while f"{base}.r{n}" in ids:
            n += 1
        return f"{base}.r{n}"

    def _current_head(self, mapping_id: str) -> str:
        """The latest row of a supersession chain starting at mapping_id."""
        by_sup = {m.get("supersedes_mapping_id"): m for m in self.data.get("mappings.csv") or []
                  if m.get("supersedes_mapping_id")}
        cur = mapping_id
        while cur in by_sup:
            cur = by_sup[cur]["mapping_id"]
        return cur

    def _supersede(self, m: dict, *, skos: Optional[str] = None, none_reason: Optional[str] = None,
                   fields: Optional[dict] = None) -> dict:
        """AIT revisions append a row that supersedes the old one; nothing is overwritten."""
        at = _ait()
        row = dict(m)
        row["mapping_id"] = self._new_mapping_id(m)
        row["supersedes_mapping_id"] = m.get("mapping_id")
        row["mapped_by"] = "human" if self.actor == "human" else "llm"
        if fields:
            row.update(fields)
            if fields.get("ait_node_id"):
                row["no_match_reason"] = ""  # the edge has a node again
        if skos:
            row["skos_relation"] = skos
        if none_reason is not None:
            row.update({"skos_relation": "none", "ait_node_id": "", "ait_cell_type_label": "", "ait_id": "",
                        "ait_hierarchy_level": "", "no_match_reason": none_reason[:300]})
        conf = at.SKOS_TO_CONFIDENCE.get(row.get("skos_relation", ""))
        if conf:
            row["match_confidence"] = conf
        row["field_provenance"] = at.add_provenance(row.get("field_provenance", ""), "skos_relation", self._prov())
        self.data.setdefault("mappings.csv", []).append(row)
        # an entity card describes one edge: same node -> the card follows the new
        # relation; another node (or none) -> the card goes (re-run ait_gene_diff)
        cards = []
        for c in self.data.get("entity_cards.csv") or []:
            if c.get("mention_id") == m.get("mention_id") and c.get("ait_node_id") == m.get("ait_node_id"):
                if row.get("ait_node_id") != m.get("ait_node_id"):
                    continue
                c["skos_relation"] = row["skos_relation"]
                c["match_confidence"] = row.get("match_confidence", c.get("match_confidence"))
            cards.append(c)
        self.data["entity_cards.csv"] = cards
        return row

    # -- driver -----------------------------------------------------------------
    def apply(self, ops: list[dict], *, round_name: Optional[str] = None) -> list[dict]:
        if not ops:
            return []  # nothing to record: an empty feedback file is not a review round
        out = []
        for op in ops or []:
            entry = {k: op.get(k) for k in ("id", "action", "field", "value", "reason", "licensed_by")
                     if op.get(k) is not None}
            entry.update({"actor": self.actor, "applied": False})
            try:
                action = op.get("action")
                if action not in ACTIONS:
                    raise OpError(f"unknown action {action!r} (one of {ACTIONS})")
                iid = str(op.get("id") or "")
                if action == "restore":
                    if self.actor != "human":
                        raise OpError("only a human restores what a gate dropped")
                    entry.update(self._restore(iid))
                else:
                    rec = self.recs.get(iid.lower()) or self.recs.get(self.alias.get(iid.lower(), ""))
                    if rec is None:
                        raise OpError(f"no item {iid!r} (ids: python -m scripts.human_feedback queue)")
                    reason = str(op.get("reason") or "")
                    if action == "drop":
                        entry.update(self._drop(rec, reason))
                    elif action == "set":
                        entry.update(self._set(rec, str(op.get("field") or ""), op.get("value")))
                    elif action == "remap":
                        entry.update(self._remap(rec, op.get("value")))
                    elif action == "demote":
                        if not rec.get("mapping"):
                            raise OpError("item has no mapping to demote")
                        entry.update(self._demote(rec, reason))
                    elif action == "approve":
                        entry.update(self._approve(rec))
                    else:
                        entry.update(self._note(rec, reason or str(op.get("value") or "")))
                entry["applied"] = True
                touched = None if action == "restore" else rec
                self._index()  # ids of later ops resolve against the updated data
                self._follow(touched)
            except OpError as e:
                entry["rejected_because"] = str(e)
            out.append(entry)
        self.log["rounds"].append({"round": round_name or f"{self.actor}-{len(self.log['rounds']) + 1}",
                                   "actor": self.actor, "by": self.by, "at": utc_now(),
                                   "applied": sum(1 for e in out if e["applied"]),
                                   "rejected": sum(1 for e in out if not e["applied"]), "ops": out})
        return out


def apply_ops(mode: str, data: dict, ops: list[dict], *, actor: str, by: Optional[str] = None,
              tools: Optional[Tools] = None, log: Optional[dict] = None,
              round_name: Optional[str] = None) -> list[dict]:
    """Apply `ops` in place; returns the per-op log (applied / rejected_because)."""
    ap = Applier(mode, data, actor=actor, by=by, tools=tools, log=log)
    out = ap.apply(ops, round_name=round_name)
    if mode == "ner":
        from group_by_entity import attach_grouped_views
        attach_grouped_views(data)
    elif mode == "ait":
        _ait().derive(data)
        data["review_sheet.csv"] = _ait().build_review_sheet(data)
    return out


# --------------------------------------------------------------------------- #
# loading / saving the three shapes
# --------------------------------------------------------------------------- #

def load(mode: str, target: Path) -> dict:
    target = Path(target)
    if mode == "ait":
        at = _ait()
        data = at.load_dir(target)
        p = target / CONCEPT_TABLE
        if p.exists():
            data[CONCEPT_TABLE] = at.read_table(p)[1]
        lp = target / LOG_FILE
        data["review_loop"] = json.loads(lp.read_text()) if lp.exists() else {"rounds": [], "dropped": {}}
        return data
    return json.loads(target.read_text())


def save(mode: str, data: dict, target: Path, *, out: Optional[Path] = None, formats=("json", "md", "ttl")) -> list[Path]:
    target = Path(target)
    if mode == "ait":
        at = _ait()
        dest = Path(out or target)
        dest.mkdir(parents=True, exist_ok=True)
        written = []
        for table in at.TABLES:
            if table in data:
                at.write_table(dest / table, table, data[table])
                written.append(dest / table)
        if CONCEPT_TABLE in data:
            import csv
            with open(dest / CONCEPT_TABLE, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
                w.writerow(CONCEPT_COLUMNS)
                for r in data[CONCEPT_TABLE]:
                    w.writerow([r.get(c, "") or "" for c in CONCEPT_COLUMNS])
            written.append(dest / CONCEPT_TABLE)
        (dest / LOG_FILE).write_text(json.dumps(data.get("review_loop") or {}, indent=1, ensure_ascii=False) + "\n")
        return written + [dest / LOG_FILE]
    if mode == "abcd":
        from scripts import abcd_export
        try:
            from scripts.abcd_verify import _coverage_audit
            data["coverage"] = _coverage_audit(data)  # items moved: the audit must follow
        except Exception:
            pass
        base = Path(out) if out else target.parent / target.name.removesuffix(".json")
        return list(abcd_export.write_all(data, base, kind="paper", formats=formats).values())
    dest = Path(out or target)
    dest.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    return [dest]


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="concept map stage for abcd/ait (trusted ontologies first)")
    m.add_argument("--mode", choices=["abcd", "ait"], required=True)
    m.add_argument("target", type=Path, help="<stem>_abcd.json, or the AIT output directory")
    m.add_argument("--offline", action="store_true", help="trusted ontology files only")
    o = sub.add_parser("ops", help="apply an operations file (actor: human unless --actor)")
    o.add_argument("--mode", choices=MODES, required=True)
    o.add_argument("target", type=Path)
    o.add_argument("--ops", type=Path, required=True)
    o.add_argument("--actor", choices=ACTORS, default="human")
    o.add_argument("--by", default=None)
    a = ap.parse_args()
    data = load(a.mode, a.target)
    if a.cmd == "map":
        map_records(a.mode, data, Tools(offline=a.offline))
        for p in save(a.mode, data, a.target):
            print(f"wrote {p}", file=sys.stderr)
        return 0
    spec = json.loads(a.ops.read_text())
    ops = spec.get("ops") if isinstance(spec, dict) else spec
    log = apply_ops(a.mode, data, ops, actor=a.actor, by=a.by)
    for e in log:
        print(("applied " if e["applied"] else "REJECTED ") + f"{e.get('action')} {e.get('id')}"
              + ("" if e["applied"] else f": {e['rejected_because']}"), file=sys.stderr)
    for p in save(a.mode, data, a.target):
        print(f"wrote {p}", file=sys.stderr)
    return 0 if all(e["applied"] for e in log) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
