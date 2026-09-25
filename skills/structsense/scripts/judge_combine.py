"""Deterministic aggregation for the judge ensemble (references/judge-ensemble.md).

No LLM call. Consumes the aligned result, one or more review files per judge
(schemas/judge-review.schema.json; a judge's review may be split across parts),
and optionally kg_plan.json. Applies, per item, in order:

  1. critical gate   any `fail` from a critical judge -> the item is DROPPED
                     (grounding / grounding_script), or the claim is dropped
                     (claims). No vote can save it: hallucinations are not averaged.
  2. mapping fail    -> demote to unmapped (ontology fields nulled,
                     concept_mapping_provenance "unmapped", alignment_method
                     "judge_demoted"). The item survives; the IRI does not.
  3. mapping tier    mapping `pass` -> mapping_tier "exactMatch"; a `flag` with
                     suggestion.tier -> that tier. json_to_ttl writes the skos
                     edge at this tier.
  4. mechanical fix  an uncontested suggestion (label / normalized_key /
                     hypothetical / negated) is applied here and logged.
  5. needs_review    conflicting verdicts, or a fail with no usable suggestion ->
                     handed to prompts/judge-combiner.md, whose output is applied
                     with --apply-fixes (only fixes some judge actually suggested).
  6. score           judge_score = sum(w_j * conf_j * s(v_j)) / sum(w_j),
                     s(pass)=1, flag=0.5, fail=0 — over the judges that reviewed it.

Every change is made on the RAW mentions (`entities[]` / `key_terms[]`), which are
the authoritative record, and the grouped views and stats are rebuilt from them —
so re-running normalize_result cannot resurrect a dropped item. Each raw mention
gets judge_score / judge_method="ensemble" / remarks; the full per-judge reviews
live once, under the top-level `judge_ensemble` block that json_to_ttl turns into
ner:ReviewDecision nodes.

Usage:
  python -m scripts.judge_combine result.json --reviews judge/reviews/*.json \
      [--kg-plan kg_plan.json] [--config judges_config.json] [-o result.json]
  python -m scripts.judge_combine result.json --apply-fixes combiner.json \
      [--kg-plan kg_plan.json] [-o result.json]

Exit codes: 0 ok, 2 input error.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from group_by_entity import attach_grouped_views, mention_groups  # noqa: E402

DEFAULT_CONFIG = _SCRIPTS_DIR.parent / "judges_config.json"
VERDICT_S = {"pass": 1.0, "flag": 0.5, "fail": 0.0}
MECHANICAL = {"label", "tier", "normalized_key", "hypothetical", "negated"}
TIERS = {"exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch"}


class InputError(ValueError):
    pass


def load_reviews(paths: list[Path]) -> dict[str, dict]:
    """judge -> merged review. Parts of one judge merge; a repeated item id is an error."""
    merged: dict[str, dict] = {}
    for p in paths:
        r = json.loads(Path(p).read_text())
        j = r.get("judge")
        if not j:
            raise InputError(f"review {p} has no 'judge'")
        into = merged.setdefault(j, {"judge": j, "model": r.get("model"), "mode": r.get("mode"),
                                     "items": [], "_ids": set()})
        for it in r.get("items") or []:
            iid = str(it.get("id", ""))
            if not iid or it.get("verdict") not in VERDICT_S:
                raise InputError(f"{p}: item {it!r} needs an id and a verdict in {sorted(VERDICT_S)}")
            if iid.lower() in into["_ids"]:
                raise InputError(f"judge '{j}' reviewed {iid!r} twice ({p})")
            into["_ids"].add(iid.lower())
            into["items"].append(it)
    for r in merged.values():
        r.pop("_ids")
    return merged


def _index(reviews: dict[str, dict]) -> dict[str, dict[str, dict]]:
    per: dict[str, dict[str, dict]] = defaultdict(dict)
    for j, r in reviews.items():
        for it in r["items"]:
            per[str(it["id"]).lower()][j] = it
    return per


def _score(revs: dict[str, dict], judges_cfg: dict) -> Optional[float]:
    num = den = 0.0
    for j, it in revs.items():
        w = float(judges_cfg.get(j, {}).get("weight", 1.0))
        conf = float(it.get("confidence", 0.5))
        num += w * conf * VERDICT_S[it["verdict"]]
        den += w
    return round(num / den, 4) if den else None


def _remarks(revs: dict[str, dict]) -> str:
    parts = [f"{j}:{it['verdict']}" for j, it in sorted(revs.items())]
    worst = next((it for it in revs.values() if it["verdict"] == "fail"), None) \
        or next((it for it in revs.values() if it["verdict"] == "flag"), None)
    tail = f" — {worst.get('reason')}" if worst and worst.get("reason") else ""
    return (", ".join(parts) + tail)[:280] or "no judge reviewed this item"


def _demote(it: dict) -> None:
    it["ontology_id"] = it["ontology_label"] = it["ontology"] = None
    it["concept_mapping_provenance"] = "unmapped"
    it["alignment_method"] = "judge_demoted"
    it.pop("mapping_tier", None)


def _plan_entry(plan: Optional[dict], gid: str) -> Optional[dict]:
    if not plan:
        return None
    ents = plan.get("entities") or {}
    for k, v in ents.items():
        if k.lower() == gid.lower():
            return v
    return None


def combine(result: dict, reviews: dict[str, dict], cfg: dict,
            kg_plan: Optional[dict] = None) -> tuple[dict, Optional[dict], dict]:
    result = copy.deepcopy(result)
    plan = copy.deepcopy(kg_plan) if kg_plan else None
    judges_cfg = cfg["judges"]
    per_item = _index(reviews)
    unknown = sorted(set(reviews) - set(judges_cfg))
    if unknown:
        raise InputError(f"reviews from judges not in the config: {unknown}")

    report: dict[str, Any] = {"dropped": [], "demoted": [], "fixes_applied": [],
                              "needs_review": [], "unreviewed": [],
                              "claims": {"dropped": [], "fixed": []}}
    reviews_out: dict[str, list[dict]] = {}
    drop_ids: set[int] = set()

    for g in mention_groups(result):
        gid = g["id"]
        revs = per_item.get(gid.lower(), {})
        items = g["items"]
        if not revs:
            report["unreviewed"].append(gid)
            continue
        score = _score(revs, judges_cfg)
        rev_list = [{"judge": j, **{k: v for k, v in it.items() if k != "id"}}
                    for j, it in sorted(revs.items())]

        crit = [j for j, it in revs.items()
                if judges_cfg.get(j, {}).get("critical") and it["verdict"] == "fail"]
        if crit:
            drop_ids.update(id(it) for it in items)
            report["dropped"].append({"id": gid, "by": crit, "mentions": len(items),
                                      "surface": g["surface"], "label": g["label"],
                                      "offsets": [[it.get("start"), it.get("end")] for it in items][:50],
                                      "reason": revs[crit[0]].get("reason")})
            reviews_out[gid] = rev_list  # kept: the provenance of the drop
            continue

        final_id = gid
        mp = revs.get("mapping")
        mapped = any(it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id") for it in items)
        if mp and mapped:
            if mp["verdict"] == "fail":
                removed = sorted({str(it.get("ontology_id")) for it in items if it.get("ontology_id")})
                for it in items:
                    _demote(it)
                report["demoted"].append({"id": gid, "from": removed, "reason": mp.get("reason")})
            else:
                tier = (mp.get("suggestion") or {}).get("tier")
                if mp["verdict"] == "flag" and tier not in TIERS:
                    tier = None
                    if (mp.get("suggestion") or {}).get("tier"):
                        report["needs_review"].append({"id": gid, "conflict": "mapping suggested an unknown tier",
                                                       "reviews": revs})
                tier = tier or ("exactMatch" if mp["verdict"] == "pass" else None)
                if tier:
                    for it in items:
                        if it.get("concept_mapping_provenance") == "tool":
                            it["mapping_tier"] = tier
                    if mp["verdict"] == "flag":
                        _mark_fix(rev_list, "mapping")
                        report["fixes_applied"].append({"id": gid, "field": "tier", "to": tier,
                                                        "licensed_by": "mapping", "applied_by": "script"})

        lab = revs.get("labeling")
        new_label = ((lab or {}).get("suggestion") or {}).get("label")
        if lab and lab["verdict"] in ("flag", "fail"):
            others_fail = [j for j, it in revs.items() if j not in ("labeling", "mapping")
                           and it["verdict"] == "fail"]
            if new_label and new_label != g["label"] and not others_fail:
                for it in items:
                    it.setdefault("label_before_judge", it.get("label"))
                    it["label"] = new_label
                final_id = gid.rsplit("|", 1)[0] + f"|{new_label}"
                _mark_fix(rev_list, "labeling")
                report["fixes_applied"].append({"id": gid, "field": "label", "from": g["label"],
                                                "to": new_label, "licensed_by": "labeling",
                                                "applied_by": "script", "new_id": final_id})
            elif lab["verdict"] == "fail":
                report["needs_review"].append({"id": gid, "conflict": "labeling fail without a usable "
                                               "suggestion" + (f", and {others_fail} also failed" if others_fail else ""),
                                               "reviews": revs})

        keys = revs.get("kg-keys")
        entry = _plan_entry(plan, gid)
        if keys and keys["verdict"] in ("flag", "fail"):
            new_key = (keys.get("suggestion") or {}).get("normalized_key")
            if new_key and entry is not None and new_key != entry.get("normalized_key"):
                report["fixes_applied"].append({"id": gid, "field": "normalized_key",
                                                "from": entry.get("normalized_key"), "to": new_key,
                                                "licensed_by": "kg-keys", "applied_by": "script"})
                _rekey(plan, entry.get("normalized_key"), new_key)
                _mark_fix(rev_list, "kg-keys")
            elif keys["verdict"] == "fail":
                report["needs_review"].append({"id": gid, "conflict": "kg-keys fail without a suggestion",
                                               "reviews": revs})

        for it in items:
            it["judge_score"] = score
            it["judge_method"] = "ensemble"
            it["remarks"] = _remarks(revs)
        reviews_out[final_id] = rev_list
        if final_id != gid:
            reviews_out[final_id].append({"judge": "combine", "verdict": "pass", "confidence": 1.0,
                                          "reason": f"relabeled from {gid!r}", "relabeled_from": gid})

    for key in ("entities", "key_terms"):
        if result.get(key):
            result[key] = [it for it in result[key] if id(it) not in drop_ids]

    if plan:
        _judge_claims(plan, reviews.get("claims"), report, reviews_out)
    _judge_extracted_claims(result, reviews.get("claims"), report, reviews_out)

    attach_grouped_views(result)
    result["judge_ensemble"] = {
        "method": "ensemble",
        "combined_at": _utc_now(),
        "combined_by": "scripts/judge_combine.py",
        "modes": sorted({r.get("mode") or "unspecified" for r in reviews.values()}),
        "judges": {j: {**{k: judges_cfg[j].get(k) for k in ("critical", "weight", "dimension", "prompt")},
                       "prompt_sha256": _file_sha256(judges_cfg[j].get("prompt")),
                       "mode": reviews[j].get("mode"), "model": reviews[j].get("model")}
                   for j in sorted(reviews)},
        "models": {j: r.get("model") for j, r in reviews.items()},
        "reviews": reviews_out,
        "report": {k: v for k, v in report.items()},
    }
    stats = result.setdefault("stats", {})
    stats["judge"] = {"method": "ensemble", "judges": sorted(reviews),
                      "models": {j: r.get("model") for j, r in reviews.items()},
                      "dropped": len(report["dropped"]), "demoted": len(report["demoted"]),
                      "fixes_applied": len(report["fixes_applied"]),
                      "needs_review": len(report["needs_review"]),
                      "unreviewed": len(report["unreviewed"])}
    _refresh_stats(result)
    return result, plan, report


def _rekey(plan: Optional[dict], old: Optional[str], new: str) -> None:
    """Rename a normalized key EVERYWHERE in the plan: every entry carrying it (a
    coreferent group shares its key) and every edge or causal slot pointing at it.
    Renaming only the entry leaves the edges dangling at the old key."""
    if not plan or not old or old == new:
        return
    for e in (plan.get("entities") or {}).values():
        if e.get("normalized_key") == old:
            e["normalized_key"] = new
        for r in e.get("relations") or []:
            if r.get("target_key") == old:
                r["target_key"] = new
        if e.get("broader_key") == old:
            e["broader_key"] = new
        for f in ("related_keys", "see_also_keys", "uses_keys", "derived_from_keys"):
            if e.get(f):
                e[f] = [new if k == old else k for k in e[f]]
    for cr in plan.get("causal_relations") or []:
        for f in ("cause_key", "effect_key"):
            if cr.get(f) == old:
                cr[f] = new
        for f in ("mediator_keys", "moderator_keys", "confounder_keys"):
            if cr.get(f):
                cr[f] = [new if k == old else k for k in cr[f]]


def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(rel: Optional[str]) -> Optional[str]:
    """sha256 of a prompt file, so a review says exactly which instructions it followed."""
    if not rel:
        return None
    import hashlib
    f = _SCRIPTS_DIR.parent / rel
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None


def _mark_fix(rev_list: list[dict], judge: str) -> None:
    for r in rev_list:
        if r["judge"] == judge:
            r["applied_fix"] = True


def _judge_claims(plan: dict, review: Optional[dict], report: dict, reviews_out: dict) -> None:
    by_id = {str(it["id"]).lower(): it for it in (review or {}).get("items") or []}
    for iid, it in by_id.items():
        reviews_out.setdefault(it["id"], []).append({"judge": "claims", **{k: v for k, v in it.items() if k != "id"}})
    kept = []
    for cr in plan.get("causal_relations") or []:
        it = by_id.get(str(cr.get("id", "")).lower())
        if it and it["verdict"] == "fail":
            report["claims"]["dropped"].append({"id": cr.get("id"), "reason": it.get("reason")})
            continue
        for f, v in ((it or {}).get("suggestion") or {}).items():
            if f in ("hypothetical", "negated") and isinstance(v, bool):
                report["claims"]["fixed"].append({"id": cr.get("id"), "field": f, "from": cr.get(f),
                                                  "to": v, "licensed_by": "claims"})
                cr[f] = v
        kept.append(cr)
    plan["causal_relations"] = kept
    for gid, e in (plan.get("entities") or {}).items():
        src = e.get("normalized_key")
        rels = []
        for r in e.get("relations") or []:
            rid = f"{src}--{r.get('predicate')}--{r.get('target_key')}".lower()
            if by_id.get(rid, {}).get("verdict") == "fail":
                report["claims"]["dropped"].append({"id": rid, "reason": by_id[rid].get("reason")})
                continue
            rels.append(r)
        if "relations" in e:
            e["relations"] = rels
        if e.get("broader_key"):
            rid = f"{src}--broader--{e['broader_key']}".lower()
            if by_id.get(rid, {}).get("verdict") == "fail":
                report["claims"]["dropped"].append({"id": rid, "reason": by_id[rid].get("reason")})
                e.pop("broader_key")
    dropped = {str(d["id"]).lower() for d in report["claims"]["dropped"]}
    for ch in plan.get("chains") or []:
        if by_id.get(str(ch.get("id", "")).lower(), {}).get("verdict") == "fail":
            ch["relation_ids"] = []
            report["claims"]["dropped"].append({"id": ch.get("id"), "reason": "chain failed claims review"})
        ch["relation_ids"] = [r for r in ch.get("relation_ids") or [] if str(r).lower() not in dropped]


def _judge_extracted_claims(result: dict, review: Optional[dict], report: dict, reviews_out: dict) -> None:
    """Claims the extractor stated (scripts/relations.py). They live in the result, so
    the verdicts are recorded (report.claims) and json_to_ttl applies them: a fail
    drops the claim, a hypothetical/negated suggestion corrects it."""
    from relations import extracted_claims
    xc = extracted_claims(result)
    ids = {r["id"].lower(): r for r in xc["relations"]} | {c["id"].lower(): c for c in xc["causal_relations"]}
    report["claims"]["extracted_unresolved"] = xc["unresolved"]
    for it in (review or {}).get("items") or []:
        c = ids.get(str(it.get("id", "")).lower())
        if c is None:
            continue
        reviews_out.setdefault(it["id"], [])
        if not any(r.get("judge") == "claims" for r in reviews_out[it["id"]]):
            reviews_out[it["id"]].append({"judge": "claims", **{k: v for k, v in it.items() if k != "id"}})
        if it.get("verdict") == "fail":
            report["claims"]["dropped"].append({"id": c["id"], "reason": it.get("reason"), "origin": "extraction"})
            continue
        for f, v in (it.get("suggestion") or {}).items():
            if f in ("hypothetical", "negated") and isinstance(v, bool) and c["id"].startswith("x-rel-"):
                report["claims"]["fixed"].append({"id": c["id"], "field": f, "from": c.get(f), "to": v,
                                                  "licensed_by": "claims", "origin": "extraction"})


def _refresh_stats(result: dict) -> None:
    try:
        from normalize_result import attach_stats
        attach_stats(result, input_path=(result.get("source_metadata") or {}).get("source_path"))
    except Exception as e:  # stats are a report, not a gate
        result.setdefault("errors", []).append(f"stats refresh failed after judging: {e}")


def apply_combiner(result: dict, combiner_out: dict, kg_plan: Optional[dict] = None,
                   combiner_model: Optional[str] = None) -> tuple[dict, Optional[dict], list[dict]]:
    """Apply the combiner's choices. A fix is applied only if some judge's review of
    that id suggested exactly that value — the combiner chooses, it never invents."""
    result = copy.deepcopy(result)
    plan = copy.deepcopy(kg_plan) if kg_plan else None
    block = result.get("judge_ensemble") or {}
    reviews = {k.lower(): v for k, v in (block.get("reviews") or {}).items()}
    groups = {g["id"].lower(): g for g in mention_groups(result)}
    log = []
    for fx in combiner_out.get("fixes_applied") or []:
        iid, field, to = str(fx.get("id", "")), fx.get("field"), fx.get("to")
        suggested = any((r.get("suggestion") or {}).get(field) == to
                        and (not fx.get("licensed_by") or r.get("judge") == fx["licensed_by"])
                        for r in reviews.get(iid.lower(), []))
        entry = {**fx, "applied": False}
        if field not in MECHANICAL or not suggested:
            entry["rejected_because"] = "no judge suggested this value" if field in MECHANICAL \
                else f"field {field!r} is not a combiner-settable field"
            log.append(entry)
            continue
        g = groups.get(iid.lower())
        if field == "label" and g:
            for it in g["items"]:
                it.setdefault("label_before_judge", it.get("label"))
                it["label"] = to
            entry["applied"] = True
        elif field == "tier" and g and to in TIERS:
            for it in g["items"]:
                if it.get("concept_mapping_provenance") == "tool":
                    it["mapping_tier"] = to
            entry["applied"] = True
        elif field == "normalized_key":
            e = _plan_entry(plan, iid)
            if e is not None:
                _rekey(plan, e.get("normalized_key"), to)
                entry["applied"] = True
        elif field in ("hypothetical", "negated") and plan:
            for cr in plan.get("causal_relations") or []:
                if str(cr.get("id", "")).lower() == iid.lower():
                    cr[field] = to
                    entry["applied"] = True
        if not entry["applied"]:
            entry.setdefault("rejected_because", "target not found")
        log.append(entry)
    attach_grouped_views(result)
    block = result.setdefault("judge_ensemble", {})
    prompt = "prompts/judge-combiner.md"
    block["combiner"] = {"fixes": log, "kept_as_is": combiner_out.get("kept_as_is") or [],
                         "escalated": combiner_out.get("escalate") or [],
                         "model": combiner_model or combiner_out.get("model"),
                         "prompt": prompt, "prompt_sha256": _file_sha256(prompt), "applied_at": _utc_now()}
    # Escalations go to the human-feedback stage (prompts/humanfeedback.md).
    result.setdefault("stats", {}).setdefault("judge", {})["escalated"] = len(block["combiner"]["escalated"])
    _refresh_stats(result)
    return result, plan, log


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", type=Path)
    ap.add_argument("--reviews", nargs="*", type=Path, default=[])
    ap.add_argument("--kg-plan", type=Path)
    ap.add_argument("--kg-plan-out", type=Path, help="default: overwrite --kg-plan")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--apply-fixes", type=Path, help="combiner output JSON (prompts/judge-combiner.md)")
    ap.add_argument("--combiner-model", help="model id that wrote the combiner output (provenance)")
    ap.add_argument("-o", "--output", type=Path, help="default: overwrite the result in place")
    ap.add_argument("--report", type=Path, help="also write the aggregation report here")
    args = ap.parse_args()

    try:
        result = json.loads(args.result.read_text())
        plan = json.loads(args.kg_plan.read_text()) if args.kg_plan else None
        if args.apply_fixes:
            out, plan_out, log = apply_combiner(result, json.loads(args.apply_fixes.read_text()), plan,
                                                combiner_model=args.combiner_model)
            applied = sum(1 for x in log if x["applied"])
            print(f"combiner: {applied} applied, {len(log) - applied} rejected, "
                  f"{len(out['judge_ensemble']['combiner']['escalated'])} escalated to human feedback",
                  file=sys.stderr)
            for x in log:
                if not x["applied"]:
                    print(f"  rejected {x.get('id')} {x.get('field')}: {x.get('rejected_because')}", file=sys.stderr)
        else:
            if not args.reviews:
                raise InputError("--reviews required (or --apply-fixes)")
            cfg = json.loads(args.config.read_text())
            out, plan_out, report = combine(result, load_reviews(args.reviews), cfg, plan)
            print(f"judges: {', '.join(sorted(out['judge_ensemble']['judges']))} | "
                  f"dropped={len(report['dropped'])} demoted={len(report['demoted'])} "
                  f"fixes={len(report['fixes_applied'])} needs_review={len(report['needs_review'])} "
                  f"unreviewed={len(report['unreviewed'])} claims_dropped={len(report['claims']['dropped'])}",
                  file=sys.stderr)
            if args.report:
                args.report.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
            if report["needs_review"]:
                print("next: run prompts/judge-combiner.md on judge_ensemble.report.needs_review, then "
                      f"python -m scripts.judge_combine {args.result} --apply-fixes <combiner.json>",
                      file=sys.stderr)
    except (InputError, json.JSONDecodeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dest = args.output or args.result
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n")
    print(f"wrote {dest}", file=sys.stderr)
    if plan_out is not None:
        pdest = args.kg_plan_out or args.kg_plan
        pdest.write_text(json.dumps(plan_out, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {pdest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
