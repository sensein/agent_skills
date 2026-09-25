"""Prepare the judge ensemble: a deterministic grounding pass + one packet per judge.

No LLM call. Given the aligned result (and the source text, and kg_plan.json if a
TTL representation is being built), writes:

  <out>/reviews/grounding_script.json
      The deterministic grounding judge. Per item: `pass` when every mention's
      offsets select its surface form; `flag` when the surface occurs in the
      source but some offsets do not anchor it (repairable); `fail` when the
      surface form occurs NOWHERE in the source (case/whitespace-insensitive) —
      a hallucinated entity, dropped by the critical gate.

  <out>/packets/<judge>/part-NNN.json
      What each LLM judge sees, and ONLY that: grounding gets the items the
      script could not pass; labeling gets surface + label + a few sentences +
      the label set in use; mapping gets the tool-mapped items; kg-keys and
      claims get the kg_plan (only when --kg-plan is given). Keeping each packet
      to its own dimension is what makes the judges independent and cheap.

Then, per packet, a judge (you, in host-model mode — one judge at a time, never
all at once) follows `prompt` and writes <out>/reviews/<judge>[-NNN].json in the
schemas/judge-review.schema.json shape. Then:

    python -m scripts.judge_combine <result> --reviews <out>/reviews/*.json [--kg-plan kg_plan.json]

Usage:
    python -m scripts.judge_prepare work/paper_final.json --source work/paper.txt \
        [--kg-plan work/kg_plan.json] [--out-dir work/judge] [--config judges_config.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from group_by_entity import mention_groups  # noqa: E402

SKILL_DIR = _SCRIPTS_DIR.parent
DEFAULT_CONFIG = SKILL_DIR / "judges_config.json"


def _norm(s: str) -> str:
    """Case/whitespace/hyphenation-insensitive form for "does it occur at all"."""
    s = re.sub(r"-\s*\n\s*", "", s or "")          # PDF line-break hyphenation
    s = s.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.sub(r"\s+", " ", s).strip().lower()


def script_grounding(groups: list[dict], text: str) -> dict:
    """The deterministic grounding review."""
    norm_text = _norm(text)
    items = []
    for g in groups:
        surf_key = g["surf_key"]
        anchored = missing = 0
        for it in g["items"]:
            s, e, surf = it.get("start"), it.get("end"), it.get(surf_key) or ""
            if isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text) \
                    and text[s:e] == surf:
                anchored += 1
            elif _norm(surf) and _norm(surf) not in norm_text:
                missing += 1
        n = len(g["items"])
        if missing == n:
            verdict, reason = "fail", f"surface form occurs nowhere in the source ({n} mention(s))"
        elif anchored == n:
            verdict, reason = "pass", f"all {n} mention offset(s) anchor the surface form"
        else:
            verdict = "flag"
            reason = (f"{anchored}/{n} mention(s) anchored; {missing} not found; "
                      f"{n - anchored - missing} found in text but offsets are off")
        items.append({"id": g["id"], "verdict": verdict, "confidence": 1.0, "reason": reason})
    return {"judge": "grounding_script", "model": "script:judge_prepare",
            "mode": "deterministic", "items": items}


def _sentences(g: dict, k: int) -> list[str]:
    out: list[str] = []
    for it in g["items"]:
        s = it.get("sentence")
        if s and s not in out:
            out.append(s if len(s) <= 600 else s[:597] + "...")
        if len(out) >= k:
            break
    return out


def _mapping_of(g: dict) -> Optional[dict]:
    for it in g["items"]:
        if it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id"):
            return {"ontology_id": it.get("ontology_id"), "ontology_label": it.get("ontology_label"),
                    "ontology": it.get("ontology")}
    return None


def build_packets(result: dict, text: str, *, kg_plan: Optional[dict], cfg: dict,
                  script_review: dict) -> dict[str, list[dict]]:
    groups = mention_groups(result)
    k = int(cfg.get("sentences_per_item", 3))
    script_verdict = {i["id"]: i for i in script_review["items"]}
    judges = cfg["judges"]
    packets: dict[str, list[dict]] = {}

    if "grounding" in judges:
        packets["grounding"] = [{
            "id": g["id"],
            "script_check": script_verdict[g["id"]]["reason"],
            "mentions": [{"surface": it.get(g["surf_key"]), "start": it.get("start"),
                          "end": it.get("end"), "sentence": it.get("sentence")}
                         for it in g["items"][:10]],
        } for g in groups if script_verdict[g["id"]]["verdict"] == "flag"]
    if "labeling" in judges:
        packets["labeling"] = [{"id": g["id"], "entity": g["surface"], "label": g["label"],
                                "sentences": _sentences(g, k)}
                               for g in groups if g["kind"] == "entity"]
    if "mapping" in judges:
        packets["mapping"] = [{"id": g["id"], "entity": g["surface"], "label": g["label"],
                               **_mapping_of(g), "sentences": _sentences(g, k)}
                              for g in groups if _mapping_of(g)]
    # claims the extractor itself stated (per-mention relations, cell_context,
    # causal_relations) are reviewed exactly like kg_plan claims
    from relations import extracted_claims
    xc = extracted_claims(result)
    by_id = {g["id"]: g for g in groups}
    x_claims = []
    for r in xc["relations"]:
        x_claims.append({"id": r["id"], "kind": "relation", "origin": r["origin"],
                         "claim": {"source": by_id[r["source"]]["surface"], "predicate": r["predicate"],
                                   "target": by_id[r["target"]]["surface"]},
                         "sentences": [r["evidence"]] if r.get("evidence") else []})
    for c in xc["causal_relations"]:
        claim = {k: v for k, v in c.items() if k not in ("id", "evidence", "origin")}
        claim["cause"], claim["effect"] = by_id[c["cause"]]["surface"], by_id[c["effect"]]["surface"]
        x_claims.append({"id": c["id"], "kind": "causal", "origin": "extraction", "claim": claim,
                         "sentences": [c["evidence"]] if c.get("evidence") else []})
    if x_claims and "claims" in judges:
        packets.setdefault("claims", []).extend(x_claims)
    if kg_plan:
        ids = {g["id"].lower(): g for g in groups}
        if "kg-keys" in judges:
            packets["kg-keys"] = [{"id": gid, "entity": ids[gid.lower()]["surface"] if gid.lower() in ids else None,
                                   "label": ids[gid.lower()]["label"] if gid.lower() in ids else None,
                                   "normalized_key": e.get("normalized_key"),
                                   "normalized_label": e.get("normalized_label"),
                                   "targets": sorted({r.get("target_key") for r in e.get("relations") or []}
                                                     | set(e.get("related_keys") or [])
                                                     | ({e["broader_key"]} if e.get("broader_key") else set())),
                                   "sentences": _sentences(ids[gid.lower()], 2) if gid.lower() in ids else []}
                                  for gid, e in (kg_plan.get("entities") or {}).items()]
        if "claims" in judges:
            claims = []
            key_to_sents = {}
            for gid, e in (kg_plan.get("entities") or {}).items():
                if gid.lower() in ids and e.get("normalized_key"):
                    key_to_sents[e["normalized_key"]] = _sentences(ids[gid.lower()], k)
            for gid, e in (kg_plan.get("entities") or {}).items():
                src = e.get("normalized_key")
                for r in e.get("relations") or []:
                    claims.append({"id": f"{src}--{r.get('predicate')}--{r.get('target_key')}",
                                   "kind": "relation", "claim": r,
                                   "sentences": key_to_sents.get(src, [])})
                if e.get("broader_key"):
                    claims.append({"id": f"{src}--broader--{e['broader_key']}", "kind": "broader",
                                   "sentences": key_to_sents.get(src, [])})
            for cr in kg_plan.get("causal_relations") or []:
                claims.append({"id": cr.get("id"), "kind": "causal", "claim": cr,
                               "sentences": key_to_sents.get(cr.get("cause_key"), [])[:2]
                               + key_to_sents.get(cr.get("effect_key"), [])[:2]})
            for ch in kg_plan.get("chains") or []:
                claims.append({"id": ch.get("id"), "kind": "chain", "claim": ch})
            packets["claims"] = packets.get("claims", []) + claims
    return {j: items for j, items in packets.items() if items}


def write_packets(packets: dict[str, list[dict]], out_dir: Path, cfg: dict,
                  labels_in_use: list[str]) -> list[Path]:
    size = max(1, int(cfg.get("batch_size", 60)))
    written = []
    for judge, items in packets.items():
        jdir = out_dir / "packets" / judge
        jdir.mkdir(parents=True, exist_ok=True)
        for old in jdir.glob("part-*.json"):
            old.unlink()
        parts = [items[i:i + size] for i in range(0, len(items), size)]
        for n, part in enumerate(parts, 1):
            packet = {
                "judge": judge,
                "prompt": cfg["judges"][judge].get("prompt"),
                "part": n, "of": len(parts),
                "review_file": f"reviews/{judge}-{n:03d}.json",
                "items": part,
            }
            if judge == "labeling":
                packet["labels_in_use"] = labels_in_use
            p = jdir / f"part-{n:03d}.json"
            p.write_text(json.dumps(packet, indent=1, ensure_ascii=False) + "\n")
            written.append(p)
    return written


def prepare(result: dict, text: str, out_dir: Path, *, kg_plan: Optional[dict] = None,
            cfg: Optional[dict] = None) -> dict:
    cfg = cfg or json.loads(DEFAULT_CONFIG.read_text())
    groups = mention_groups(result)
    review = script_grounding(groups, text)
    (out_dir / "reviews").mkdir(parents=True, exist_ok=True)
    (out_dir / "reviews" / "grounding_script.json").write_text(
        json.dumps(review, indent=1, ensure_ascii=False) + "\n")
    packets = build_packets(result, text, kg_plan=kg_plan, cfg=cfg, script_review=review)
    labels = sorted({g["label"] for g in groups if g["label"]})
    files = write_packets(packets, out_dir, cfg, labels)
    verdicts = {v: sum(1 for i in review["items"] if i["verdict"] == v) for v in ("pass", "flag", "fail")}
    return {"groups": len(groups), "grounding_script": verdicts,
            "packets": {j: len(v) for j, v in packets.items()},
            "files": [str(f) for f in files]}


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", type=Path)
    ap.add_argument("--source", type=Path, required=True, help="the text the extractor read (<stem>.txt)")
    ap.add_argument("--kg-plan", type=Path)
    ap.add_argument("--out-dir", type=Path, help="default: <result dir>/judge")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args()
    result = json.loads(args.result.read_text())
    plan = json.loads(args.kg_plan.read_text()) if args.kg_plan else None
    out = args.out_dir or args.result.parent / "judge"
    summary = prepare(result, args.source.read_text(), out, kg_plan=plan,
                      cfg=json.loads(args.config.read_text()))
    g = summary["grounding_script"]
    print(f"{summary['groups']} items; grounding_script pass={g['pass']} flag={g['flag']} fail={g['fail']}",
          file=sys.stderr)
    for j, n in summary["packets"].items():
        print(f"  packet {j}: {n} item(s)", file=sys.stderr)
    print(f"next: for each packet, follow its `prompt` and write its `review_file` under {out}; "
          f"one judge at a time. Then: python -m scripts.judge_combine {args.result} "
          f"--reviews {out}/reviews/*.json" + (f" --kg-plan {args.kg_plan}" if args.kg_plan else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
