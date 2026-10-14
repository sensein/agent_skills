"""The judge ensemble for record modes (ABCD/HBCD, AIT) — same panel, same gates.

NER is judged by judge_prepare.py / judge_combine.py. ABCD and AIT records are not
mentions, so they get their own packets, but the SAME judges (judges_config.json),
the same review schema, the same critical gates, and one generic prompt
(prompts/judge-record.md) that tells each judge its dimension. Verdicts become
review_loop operations, so a judge corrects through exactly the vocabulary a human
does — and through nothing else:

    critical fail (grounding_script / grounding / claims)  -> drop
    mapping fail                                          -> demote (+ remap if the
                                                             judge gave a better query:
                                                             the TOOL picks the id)
    mapping flag + tier                                   -> set tier / skos_relation
    labeling / claims flag|fail + suggestion.set          -> set (allowlisted fields only)
    anything contested or unfixable                       -> needs_review -> combiner
                                                             -> escalate -> human feedback

    python -m scripts.record_judge prepare --mode abcd out/paper_abcd.json [--out-dir judge/]
    #   ... one judge at a time: follow each packet's `prompt`, write its `review_file` ...
    python -m scripts.record_judge combine --mode abcd out/paper_abcd.json --reviews judge/reviews/*.json
    python -m scripts.record_judge combine --mode abcd out/paper_abcd.json --apply-fixes combiner.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = _SCRIPTS_DIR.parent
for _p in (str(_SCRIPTS_DIR), str(SKILL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_loop as rl  # noqa: E402

logger = logging.getLogger("record_judge")
DEFAULT_CONFIG = SKILL_DIR / "judges_config.json"
VERDICT_S = {"pass": 1.0, "flag": 0.5, "fail": 0.0}
PROMPT = "prompts/judge-record.md"

DIMENSIONS = {
    "grounding": "Is the item really stated by THIS paper? The quote/sentences are the only evidence. "
                 "fail = not supported (hallucinated, cited work, or a different thing); pass = supported.",
    "labeling": "Do the item's typed fields (`fields`) fit what the evidence says? Suggest a corrected value "
                "ONLY from `allowed` (or, where a field says in_quote, a value copied from the quote).",
    "mapping": "Does the mapped id/label (`mapping`) MEAN the same thing as the item? pass = same concept; "
               "flag + suggestion.tier = near/broader/narrower; fail = a different concept. On fail you may give "
               "suggestion.query, a better SEARCH TERM taken from the paper — never an id: the tool decides.",
    "claims": "Is the finding (statement, direction, effect size, statistic) what THIS paper's own analysis "
              "reports in the quote? fail = not this study's result or not supported; flag + suggestion.set "
              "for a wrong direction/effect copied from the quote.",
}


def load_cfg(path: Optional[Path] = None) -> dict:
    return json.loads(Path(path or DEFAULT_CONFIG).read_text())


def _abcd_ground(rec: dict) -> tuple[str, str]:
    ev = rec.get("evidence") or {}
    if not ev.get("quote"):
        return "fail", "no evidence quote"
    if ev.get("anchor_method") == "re_anchored":
        return "flag", (f"quote found in the paper but not at the reported offsets "
                        f"({ev.get('occurrences') or '?'} occurrence(s)); the extractor miscounted or misplaced it")
    return "pass", "abcd_verify anchored the quote at its offsets"


def _ait_ground(rec: dict) -> tuple[str, str]:
    st = (rec.get("evidence") or {}).get("verified") or []
    if not st:
        return "flag", "no evidence verification recorded"
    if all(s == "not_found" for s in st):
        return "fail", "no evidence sentence was found in the source (ait_evidence)"
    if any(s in ("exact", "exact_offset_corrected") for s in st):
        return "pass", f"{sum(s.startswith('exact') for s in st)}/{len(st)} evidence sentence(s) exact"
    return "flag", "evidence only fuzzy-matched; check it supports the entity"


def script_grounding(mode: str, recs: list[dict]) -> dict:
    items = []
    for r in recs:
        if mode == "abcd" or (mode == "ait" and r["kind"] == "entity"):
            v, why = (_abcd_ground if mode == "abcd" else _ait_ground)(r)
            items.append({"id": r["id"], "verdict": v, "confidence": 1.0, "reason": why})
    return {"judge": "grounding_script", "model": "script:record_judge", "mode": "deterministic", "items": items}


def _allowed(mode: str, kind: str) -> dict:
    out = {}
    for f, a in (rl.FIXABLE.get(mode) or {}).get(kind, {}).items():
        if f == "tier":
            continue
        out[f] = list(a) if isinstance(a, (list, tuple)) else a
    return out


def _evidence(r: dict) -> dict:
    ev = r.get("evidence") or {}
    return {k: v for k, v in ev.items() if v not in (None, "", [])}


def build_packets(mode: str, recs: list[dict], script: dict, cfg: dict) -> dict[str, list[dict]]:
    judges = cfg["judges"]
    sv = {i["id"]: i for i in script["items"]}
    p: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        base = {"id": r["id"], "kind": r["kind"], "item": r["surface"], "evidence": _evidence(r)}
        s = sv.get(r["id"])
        if "grounding" in judges and s and s["verdict"] == "flag":
            p["grounding"].append({**base, "script_check": s["reason"]})
        allowed = _allowed(mode, r["kind"])
        labeled = {k: v for k, v in r["fields"].items() if k in allowed}
        if "labeling" in judges and labeled and r["kind"] in ("variable", "construct", "model", "entity"):
            p["labeling"].append({**base, "fields": labeled, "allowed": allowed})
        if "mapping" in judges and r.get("mapping"):
            p["mapping"].append({**base, "mapping": r["mapping"]})
        if "claims" in judges and r["kind"] == "finding":
            ref = r["ref"]
            p["claims"].append({**base, "fields": labeled, "allowed": allowed,
                                "claim": {k: ref.get(k) for k in ("statement", "direction", "variables", "construct",
                                                                  "effect_size", "statistic", "subgroup", "analytic_n")
                                          if ref.get(k) not in (None, "", [])}})
    return {j: v for j, v in p.items() if v}


def prepare(mode: str, data: dict, out_dir: Path, cfg: Optional[dict] = None) -> dict:
    cfg = cfg or load_cfg()
    recs = rl.records(mode, data)
    script = script_grounding(mode, recs)
    (out_dir / "reviews").mkdir(parents=True, exist_ok=True)
    (out_dir / "reviews" / "grounding_script.json").write_text(json.dumps(script, indent=1, ensure_ascii=False) + "\n")
    packets = build_packets(mode, recs, script, cfg)
    size = max(1, int(cfg.get("batch_size", 60)))
    files = []
    for judge, items in packets.items():
        jdir = out_dir / "packets" / judge
        jdir.mkdir(parents=True, exist_ok=True)
        for old in jdir.glob("part-*.json"):
            old.unlink()
        parts = [items[i:i + size] for i in range(0, len(items), size)]
        for n, part in enumerate(parts, 1):
            f = jdir / f"part-{n:03d}.json"
            f.write_text(json.dumps({"judge": judge, "mode_of_items": mode, "prompt": PROMPT,
                                     "dimension": DIMENSIONS[judge], "part": n, "of": len(parts),
                                     "review_file": f"reviews/{judge}-{n:03d}.json", "items": part},
                                    indent=1, ensure_ascii=False) + "\n")
            files.append(str(f))
    v = {k: sum(1 for i in script["items"] if i["verdict"] == k) for k in VERDICT_S}
    return {"records": len(recs), "grounding_script": v, "packets": {j: len(x) for j, x in packets.items()},
            "files": files}


def load_reviews(paths: list[Path]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for p in paths:
        r = json.loads(Path(p).read_text())
        j = r.get("judge")
        if not j:
            raise ValueError(f"review {p} has no 'judge'")
        into = merged.setdefault(j, {"judge": j, "model": r.get("model"), "mode": r.get("mode"), "items": []})
        seen = {str(i["id"]).lower() for i in into["items"]}
        for it in r.get("items") or []:
            if it.get("verdict") in VERDICT_S and str(it.get("id", "")).lower() not in seen:
                into["items"].append(it)
                seen.add(str(it["id"]).lower())
    return merged


def _tier_op(mode: str, rec: dict, tier: str) -> Optional[dict]:
    if mode == "ait" and rec["kind"] == "mapping":
        return {"id": rec["id"], "action": "set", "field": "skos_relation", "value": f"skos:{tier}",
                "licensed_by": "mapping"}
    if "tier" in (rl.FIXABLE.get(mode) or {}).get(rec["kind"], {}):
        return {"id": rec["id"], "action": "set", "field": "tier", "value": tier, "licensed_by": "mapping"}
    return None


def verdicts_to_ops(mode: str, recs: list[dict], reviews: dict[str, dict], cfg: dict) -> tuple[list, list, dict]:
    jcfg = cfg["judges"]
    per: dict[str, dict[str, dict]] = defaultdict(dict)
    for j, r in reviews.items():
        for it in r["items"]:
            per[str(it["id"]).lower()][j] = it
    ops, needs, scores = [], [], {}
    for rec in recs:
        revs = per.get(str(rec["id"]).lower())
        if not revs:
            continue
        num = den = 0.0
        for j, it in revs.items():
            w = float(jcfg.get(j, {}).get("weight", 1.0))
            num += w * float(it.get("confidence", 0.5)) * VERDICT_S[it["verdict"]]
            den += w
        scores[rec["id"]] = {"judge_score": round(num / den, 4) if den else None,
                             "remarks": ", ".join(f"{j}:{it['verdict']}" for j, it in sorted(revs.items()))}
        crit = [j for j, it in revs.items() if jcfg.get(j, {}).get("critical") and it["verdict"] == "fail"]
        if crit:
            ops.append({"id": rec["id"], "action": "drop", "licensed_by": crit[0],
                        "reason": f"{crit[0]}: {revs[crit[0]].get('reason', '')}"[:300]})
            continue
        mp = revs.get("mapping")
        if mp and rec.get("mapping"):
            sug = mp.get("suggestion") or {}
            if mp["verdict"] == "fail":
                ops.append({"id": rec["id"], "action": "demote", "licensed_by": "mapping",
                            "reason": mp.get("reason", "")[:300]})
                if sug.get("query"):
                    ops.append({"id": rec["id"], "action": "remap", "value": sug["query"], "licensed_by": "mapping"})
            elif mp["verdict"] == "flag":
                tier = sug.get("tier")
                op = _tier_op(mode, rec, tier) if tier in rl.TIERS else None
                if op:
                    ops.append(op)
                else:
                    needs.append({"id": rec["id"], "reviews": revs,
                                  "conflict": ("mapping flagged: " + (mp.get("reason") or "")[:160]
                                               + ("" if tier in rl.TIERS else
                                                  " (no tier suggested)" if not tier else f" (tier {tier!r} unknown)")
                                               + ("; a dictionary match has no tier — a human decides (remap/demote)"
                                                  if rec["kind"] == "variable" else ""))})
        others_fail = [j for j, it in revs.items() if j not in ("labeling", "claims", "mapping") and it["verdict"] == "fail"]
        for j in ("labeling", "claims"):
            it = revs.get(j)
            if not it or it["verdict"] == "pass":
                continue
            sets = (it.get("suggestion") or {}).get("set") or {}
            if sets and not others_fail:
                ops.extend({"id": rec["id"], "action": "set", "field": f, "value": v, "licensed_by": j}
                           for f, v in sets.items())
            elif it["verdict"] == "fail" or sets:
                needs.append({"id": rec["id"], "conflict": f"{j} {it['verdict']} without an uncontested suggestion",
                              "reviews": revs})
    return ops, needs, scores


def _attach_scores(mode: str, data: dict, recs: list[dict], scores: dict) -> None:
    by = {r["id"]: r for r in recs}
    for iid, s in scores.items():
        r = by.get(iid)
        if r is None:
            continue
        if mode == "abcd":
            r["ref"]["judge_score"], r["ref"]["judge_remarks"] = s["judge_score"], s["remarks"]
    data.setdefault("review_loop", {}).setdefault("scores", {}).update(scores)


def combine(mode: str, data: dict, reviews: dict[str, dict], cfg: Optional[dict] = None,
            tools: Optional[rl.Tools] = None) -> dict:
    cfg = cfg or load_cfg()
    unknown = sorted(set(reviews) - set(cfg["judges"]))
    if unknown:
        raise ValueError(f"reviews from judges not in the config: {unknown}")
    recs = rl.records(mode, data)
    ops, needs, scores = verdicts_to_ops(mode, recs, reviews, cfg)
    _attach_scores(mode, data, recs, scores)
    log = rl.apply_ops(mode, data, ops, actor="judge", by="scripts/record_judge.py", tools=tools,
                       round_name="judge-combine")
    block = data.setdefault("review_loop", {})
    block["judge"] = {"combined_at": rl.utc_now(), "mode": mode, "prompt": PROMPT,
                      "judges": {j: {"model": r.get("model"), "mode": r.get("mode"),
                                     **{k: cfg["judges"][j].get(k) for k in ("critical", "weight", "dimension")}}
                                 for j, r in sorted(reviews.items())},
                      "reviews": {j: r["items"] for j, r in reviews.items()},
                      "needs_review": needs,
                      "summary": {"ops": len(log), "applied": sum(e["applied"] for e in log),
                                  "rejected": sum(not e["applied"] for e in log), "needs_review": len(needs),
                                  "dropped": sum(e["applied"] and e["action"] == "drop" for e in log)}}
    return block["judge"]["summary"]


def apply_combiner(mode: str, data: dict, combiner_out: dict, tools: Optional[rl.Tools] = None,
                   model: Optional[str] = None) -> list[dict]:
    """The combiner chooses among judges' suggestions; anything no judge proposed is refused."""
    block = data.setdefault("review_loop", {})
    reviews = (block.get("judge") or {}).get("reviews") or {}
    ops = []
    for fx in combiner_out.get("fixes_applied") or []:
        iid, field, to, lic = str(fx.get("id", "")), fx.get("field"), fx.get("to"), fx.get("licensed_by")
        licensed = any(str(it.get("id", "")).lower() == iid.lower()
                       and (((it.get("suggestion") or {}).get("set") or {}).get(field) == to
                            or (field == "tier" and (it.get("suggestion") or {}).get("tier") == to)
                            or (field == "query" and (it.get("suggestion") or {}).get("query") == to))
                       for j, items in reviews.items() if not lic or j == lic for it in items)
        if not licensed:
            ops.append({"id": iid, "action": "note", "reason": f"combiner proposed {field}={to!r}, refused: "
                                                                "no judge suggested it"})
            continue
        if field == "query":
            ops.append({"id": iid, "action": "remap", "value": to, "licensed_by": lic})
        elif field == "tier":
            rec = next((r for r in rl.records(mode, data) if r["id"].lower() == iid.lower()), None)
            op = _tier_op(mode, rec, to) if rec else None
            if op:
                ops.append(op)
        else:
            ops.append({"id": iid, "action": "set", "field": field, "value": to, "licensed_by": lic})
    log = rl.apply_ops(mode, data, ops, actor="combiner", by=model, tools=tools, round_name="combiner")
    block["escalated"] = (block.get("escalated") or []) + list(combiner_out.get("escalate") or [])
    return log


def system_prompt() -> str:
    text = (SKILL_DIR / PROMPT).read_text()
    return text.split("## System", 1)[1].split("```", 2)[1].strip()


def run_panel(mode: str, data: dict, *, call: Callable[..., str], default_model: str,
              judge_models: Optional[dict] = None, combiner_model: Optional[str] = None,
              work_dir: Path, cfg: Optional[dict] = None, tools: Optional[rl.Tools] = None,
              source_text: Optional[str] = None) -> dict:
    """Framework mode: one call per packet part, then combine (and the combiner)."""
    from json_repair import parse_or_repair
    cfg = cfg or load_cfg()
    summary = prepare(mode, data, work_dir, cfg)
    for path in sorted((work_dir / "packets").glob("*/part-*.json")):
        packet = json.loads(path.read_text())
        judge = packet["judge"]
        model = (judge_models or {}).get(judge) or default_model
        user = f"PACKET:\n{json.dumps(packet, ensure_ascii=False)}"
        if judge == "grounding" and source_text:
            user += f"\n\nSOURCE TEXT:\n{source_text}"
        raw = call(model=model, system=system_prompt(), user=user, json_mode=True, temperature=0)
        review = parse_or_repair(raw) or {}
        ids = {i["id"] for i in packet["items"]}
        items = [i for i in review.get("items") or [] if isinstance(i, dict) and i.get("id") in ids
                 and i.get("verdict") in VERDICT_S]
        (work_dir / "reviews" / Path(packet["review_file"]).name).write_text(json.dumps(
            {"judge": judge, "model": f"llm:{model}", "mode": "parallel", "items": items}, indent=1) + "\n")
    reviews = load_reviews(sorted((work_dir / "reviews").glob("*.json")))
    out = combine(mode, data, reviews, cfg, tools)
    needs = data["review_loop"]["judge"]["needs_review"]
    if needs and combiner_model:
        from judge_ensemble import system_prompt as sp
        raw = call(model=combiner_model, system=sp(cfg["combiner"]["prompt"]),
                   user=json.dumps({"needs_review": needs}, ensure_ascii=False, default=str),
                   json_mode=True, temperature=0)
        apply_combiner(mode, data, parse_or_repair(raw) or {}, tools, combiner_model)
    elif needs:
        data["review_loop"]["escalated"] = (data["review_loop"].get("escalated") or []) + [
            {"id": n["id"], "question": n["conflict"]} for n in needs]
    out["prepare"] = summary
    return out


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--mode", choices=["abcd", "ait"], required=True)
    p.add_argument("target", type=Path)
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    c = sub.add_parser("combine")
    c.add_argument("--mode", choices=["abcd", "ait"], required=True)
    c.add_argument("target", type=Path)
    c.add_argument("--reviews", nargs="*", type=Path, default=[])
    c.add_argument("--apply-fixes", type=Path, help="combiner output (prompts/judge-combiner.md)")
    c.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    c.add_argument("--offline", action="store_true", help="remaps use the trusted ontology files only")
    a = ap.parse_args()
    data = rl.load(a.mode, a.target)
    default_dir = (a.target if a.mode == "ait" else a.target.parent) / "judge" / \
        (a.target.name.removesuffix(".json") if a.mode == "abcd" else "")
    if a.cmd == "prepare":
        out = a.out_dir or default_dir
        s = prepare(a.mode, data, out, load_cfg(a.config))
        if a.mode == "abcd":  # persist the review ids the packets refer to
            rl.save(a.mode, data, a.target, formats=("json",))
        print(json.dumps({k: v for k, v in s.items() if k != "files"}), file=sys.stderr)
        print(f"next: for each packet under {out}/packets, follow {PROMPT} as that judge (one judge at a "
              f"time) and write its review_file; then python -m scripts.record_judge combine --mode {a.mode} "
              f"{a.target} --reviews {out}/reviews/*.json", file=sys.stderr)
        return 0
    tools = rl.Tools(offline=a.offline)
    if a.apply_fixes:
        log = apply_combiner(a.mode, data, json.loads(a.apply_fixes.read_text()), tools)
        print(f"combiner: {sum(e['applied'] for e in log)} applied, {sum(not e['applied'] for e in log)} refused",
              file=sys.stderr)
    else:
        reviews = load_reviews(a.reviews or sorted((default_dir / "reviews").glob("*.json")))
        print(json.dumps(combine(a.mode, data, reviews, load_cfg(a.config), tools)), file=sys.stderr)
        if data["review_loop"]["judge"]["needs_review"]:
            print("next: prompts/judge-combiner.md over review_loop.judge.needs_review, then "
                  f"--apply-fixes; or hand them to the human queue (python -m scripts.human_feedback queue)",
                  file=sys.stderr)
    for f in rl.save(a.mode, data, a.target):
        print(f"wrote {f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
