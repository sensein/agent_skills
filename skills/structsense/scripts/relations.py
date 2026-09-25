"""Relations and causal claims that come out of EXTRACTION — resolved to entities.

Every NER prompt (general / neuroscience / cns-cells) lets the extractor state, per
mention, the relations the text asserts about it (`relations`: predicate + target as
written), and, per document, the causal claims the paper argues (`causal_relations`:
cause / effect / mediators as written). The cns-cells prompt's `cell_context`
(markers, region, layer, species, ephys) is relational too. This module turns all of
that into claims between extracted ENTITIES — deterministically, no LLM call:

  - a target string is resolved to the group of an extracted mention in the same
    paper (same sentence first, then anywhere, case/whitespace-insensitive, then by
    folded key); an unresolvable target is reported, never invented;
  - the predicate must be in ttl_config.json `relation_predicates`, or `broader`
    (the in-paper hierarchy, written as skos:broader);
  - every claim carries the sentence it came from as evidence.

The claims judge reviews these like kg_plan claims (judge_prepare.py packets, ids
`<src group id>--<predicate>--<tgt group id>` and `x-rel-N` for causal ones);
judge_combine.py prunes the ones it fails; json_to_ttl.py writes the survivors. A
kg_plan.json relation for the same pair is not duplicated.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from group_by_entity import mention_groups  # noqa: E402

TTL_CONFIG = _SCRIPTS_DIR.parent / "default_ontology" / "ttl_config.json"
HIERARCHY = "broader"

# cns-cells `cell_context` field -> relation predicate (short names from ttl_config)
CELL_CONTEXT_PREDICATES = {
    "lineage_markers": "expresses",
    "region": "located_in",
    "layer": "located_in",
    "species": "in_taxon",
    "ephys_summary": "has_phenotype",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def allowed_predicates(ttl_config: Path = TTL_CONFIG) -> set[str]:
    cfg = json.loads(Path(ttl_config).read_text())
    rp = {k for k in (cfg.get("relation_predicates") or {}) if not k.startswith("_")}
    return rp | {HIERARCHY}


class Resolver:
    """Surface string -> group id of an extracted mention in this paper."""

    def __init__(self, result: dict):
        self.groups = mention_groups(result)
        self.by_surface: dict[str, list[dict]] = {}
        self.by_sentence: dict[tuple[str, str], dict] = {}
        for g in self.groups:
            for it in g["items"]:
                surf = _norm(it.get(g["surf_key"]) or "")
                if not surf:
                    continue
                lst = self.by_surface.setdefault(surf, [])
                if g not in lst:
                    lst.append(g)
                if it.get("sentence"):
                    self.by_sentence.setdefault((surf, it["sentence"]), g)

    def group_of_item(self, item: dict, surf_key: str) -> Optional[dict]:
        key = _norm(item.get(surf_key) or "")
        for g in self.by_surface.get(key, []):
            if item in g["items"]:
                return g
        return None

    def resolve(self, target: str, sentence: Optional[str] = None,
                exclude: Optional[dict] = None) -> Optional[dict]:
        t = _norm(target)
        if not t:
            return None
        if sentence and (t, sentence) in self.by_sentence and self.by_sentence[(t, sentence)] is not exclude:
            return self.by_sentence[(t, sentence)]
        cands = [g for g in self.by_surface.get(t, []) if g is not exclude]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:  # prefer the group with the most mentions (the paper's usage)
            return max(cands, key=lambda g: len(g["items"]))
        # plural/singular tolerance ("basket cells" vs "basket cell")
        for alt in ({t[:-1]} if t.endswith("s") else {t + "s"}):
            cands = [g for g in self.by_surface.get(alt, []) if g is not exclude]
            if cands:
                return max(cands, key=lambda g: len(g["items"]))
        return None


def extracted_claims(result: dict, ttl_config: Path = TTL_CONFIG) -> dict:
    """{"relations": [...], "causal_relations": [...], "unresolved": [...]} from the
    extractor's own relational output. Relations are deduplicated by
    (src group, predicate, tgt group); each keeps the first sentence as evidence."""
    allowed = allowed_predicates(ttl_config)
    res = Resolver(result)
    rels: dict[tuple[str, str, str], dict] = {}
    unresolved: list[dict] = []

    def add(src: dict, pred: str, target: str, sentence: Optional[str], origin: str):
        if pred not in allowed:
            unresolved.append({"source": src["id"], "predicate": pred, "target": target,
                               "why": f"predicate not in ttl_config.json relation_predicates ({origin})"})
            return
        tgt = res.resolve(target, sentence, exclude=src)
        if tgt is None:
            unresolved.append({"source": src["id"], "predicate": pred, "target": target,
                               "why": f"target is not an extracted mention in this paper ({origin})"})
            return
        key = (src["id"], pred, tgt["id"])
        if key not in rels:
            rels[key] = {"id": f"{src['id']}--{pred}--{tgt['id']}", "source": src["id"],
                         "predicate": pred, "target": tgt["id"], "evidence": sentence, "origin": origin}

    for g in res.groups:
        for it in g["items"]:
            sent = it.get("sentence")
            for r in it.get("relations") or []:
                if isinstance(r, dict) and r.get("predicate") and r.get("target"):
                    add(g, str(r["predicate"]).strip(), str(r["target"]), r.get("evidence") or sent, "extraction")
            if it.get("broader"):
                add(g, HIERARCHY, str(it["broader"]), sent, "extraction")
            ctx = it.get("cell_context") or {}
            if isinstance(ctx, dict):
                for field, pred in CELL_CONTEXT_PREDICATES.items():
                    vals = ctx.get(field)
                    for v in (vals if isinstance(vals, list) else [vals]):
                        if v:
                            add(g, pred, str(v), sent, f"cell_context.{field}")

    causal = []
    for n, cr in enumerate(result.get("causal_relations") or [], 1):
        if not isinstance(cr, dict):
            continue
        sent = cr.get("evidence") or cr.get("sentence")
        cause = res.resolve(str(cr.get("cause") or ""), cr.get("sentence"))
        effect = res.resolve(str(cr.get("effect") or ""), cr.get("sentence"))
        if cause is None or effect is None or cause is effect:
            unresolved.append({"source": cr.get("cause"), "predicate": "causes", "target": cr.get("effect"),
                               "why": "cause or effect is not an extracted mention (causal_relations)"})
            continue
        out = {"id": f"x-rel-{n}", "cause": cause["id"], "effect": effect["id"], "evidence": sent,
               "origin": "extraction"}
        for field in ("mediators", "moderators", "confounders"):
            gs = [res.resolve(str(x), cr.get("sentence")) for x in cr.get(field) or []]
            out[field] = [g["id"] for g in gs if g is not None]
        for field in ("type", "polarity", "modality", "directness", "evidence_basis", "hypothetical",
                      "negated", "strength", "effect_estimate"):
            if cr.get(field) is not None:
                out[field] = cr[field]
        causal.append(out)
    return {"relations": list(rels.values()), "causal_relations": causal, "unresolved": unresolved}
