"""Framework-mode runner for the judge ensemble and the KG plan (one API call per packet).

Host-model mode does not need this file: the agent IS every judge and follows
references/judge-ensemble.md by hand (judge_prepare -> one judge at a time ->
judge_combine). This is the same procedure for a headless run, where each panel
member can be a different — ideally cheaper, ideally different-family — model:

    prepare packets (judge_prepare, deterministic grounding review)
      -> for each judge, for each packet part: one call with prompts/judge-<j>.md
      -> judge_combine (gates, demotions, mechanical fixes, scores)
      -> combiner model, only if needs_review is non-empty -> apply_combiner

`make_kg_plan` writes kg_plan.json from prompts/kg-plan.md in one call over the
grouped items and their sentences.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from group_by_entity import mention_groups  # noqa: E402
from json_repair import parse_or_repair  # noqa: E402
from judge_combine import apply_combiner, combine, load_reviews  # noqa: E402
from judge_prepare import DEFAULT_CONFIG, prepare  # noqa: E402

logger = logging.getLogger("judge_ensemble")
SKILL_DIR = _SCRIPTS_DIR.parent


def system_prompt(rel_path: str) -> str:
    """The fenced block under `## System` of a prompt file."""
    text = (SKILL_DIR / rel_path).read_text()
    after = text.split("## System", 1)[1]
    body = after.split("```", 2)[1]
    return body.lstrip("\n").strip()


def run_panel(result: dict, text: str, *, call: Callable[..., str], default_model: str,
              judge_models: Optional[dict[str, str]] = None, combiner_model: Optional[str] = None,
              kg_plan: Optional[dict] = None, work_dir: Path, cfg: Optional[dict] = None
              ) -> tuple[dict, Optional[dict], dict]:
    """Run every configured judge over its packets, then combine. `call(model=, system=,
    user=, json_mode=, temperature=)` is llm_client.call."""
    cfg = cfg or json.loads(DEFAULT_CONFIG.read_text())
    judge_models = judge_models or {}
    summary = prepare(result, text, work_dir, kg_plan=kg_plan, cfg=cfg)
    reviews_dir = work_dir / "reviews"
    for packet_path in sorted((work_dir / "packets").glob("*/part-*.json")):
        packet = json.loads(packet_path.read_text())
        judge = packet["judge"]
        spec = cfg["judges"][judge]
        model = judge_models.get(judge) or default_model
        user = f"PACKET:\n{json.dumps(packet, ensure_ascii=False)}"
        if judge in ("grounding", "claims"):
            user += f"\n\nSOURCE TEXT:\n{text}"
        raw = call(model=model, system=system_prompt(spec["prompt"]), user=user,
                   json_mode=True, temperature=0)
        review = parse_or_repair(raw) or {}
        ids = {it["id"] for it in packet["items"] if it.get("id")}
        items = [it for it in review.get("items") or []
                 if isinstance(it, dict) and it.get("id") in ids and it.get("verdict") in ("pass", "flag", "fail")]
        missing = len(ids) - len({it["id"] for it in items})
        if missing:
            logger.warning("%s part %s: %d item(s) not reviewed", judge, packet.get("part"), missing)
        (reviews_dir / Path(packet["review_file"]).name).write_text(json.dumps(
            {"judge": judge, "model": f"llm:{model}", "mode": "parallel", "items": items},
            indent=1, ensure_ascii=False) + "\n")
    reviews = load_reviews(sorted(reviews_dir.glob("*.json")))
    judged, plan, report = combine(result, reviews, cfg, kg_plan)
    if report["needs_review"] and combiner_model:
        user = json.dumps({"needs_review": report["needs_review"]}, ensure_ascii=False, default=str)
        raw = call(model=combiner_model, system=system_prompt(cfg["combiner"]["prompt"]), user=user,
                   json_mode=True, temperature=0)
        decisions = parse_or_repair(raw) or {}
        judged, plan, log = apply_combiner(judged, decisions, plan, combiner_model=combiner_model)
        report["combiner"] = log
    report["prepare"] = summary
    return judged, plan, report


def make_kg_plan(result: dict, *, call: Callable[..., str], model: str, max_items: int = 400) -> dict:
    """kg_plan.json from prompts/kg-plan.md, over grouped items and their sentences."""
    groups = mention_groups(result)[:max_items]
    items = []
    for g in groups:
        it = g["items"][0]
        items.append({"id": g["id"], "label": g["label"], "mentions": len(g["items"]),
                      "ontology_id": it.get("ontology_id") if it.get("concept_mapping_provenance") == "tool" else None,
                      "ontology_label": it.get("ontology_label"),
                      "sentences": list(dict.fromkeys(i.get("sentence") for i in g["items"] if i.get("sentence")))[:3]})
    raw = call(model=model, system=system_prompt("prompts/kg-plan.md"),
               user=json.dumps({"items": items}, ensure_ascii=False), json_mode=True, temperature=0)
    plan = parse_or_repair(raw) or {}
    return sanitize_kg_plan(plan)


def sanitize_kg_plan(plan) -> dict:
    """Keep only the parts of a model-written plan that match schemas/kg-plan.schema.json.
    A malformed plan must degrade to a flatter TTL, never crash the run."""
    if not isinstance(plan, dict):
        logger.warning("kg_plan is not an object; ignored")
        return {}
    out: dict = {}
    ents = plan.get("entities")
    if isinstance(ents, dict):
        out["entities"] = {k: v for k, v in ents.items() if isinstance(k, str) and isinstance(v, dict)}
    elif ents is not None:
        logger.warning("kg_plan.entities is not an object keyed by '<entity>|<label>'; ignored")
    for field in ("causal_relations", "chains"):
        vals = plan.get(field)
        if isinstance(vals, list):
            out[field] = [v for v in vals if isinstance(v, dict)]
    try:
        import jsonschema
        schema = json.loads((SKILL_DIR / "schemas" / "kg-plan.schema.json").read_text())
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(out))
        for e in errors[:5]:
            logger.warning("kg_plan: %s at %s", e.message, "/".join(map(str, e.absolute_path)))
        for e in errors:  # drop the offending entry, keep the rest
            path = list(e.absolute_path)
            if len(path) >= 2 and path[0] == "entities":
                out["entities"].pop(path[1], None)
            elif len(path) >= 2 and path[0] in ("causal_relations", "chains") and isinstance(path[1], int) \
                    and path[1] < len(out[path[0]]):
                out[path[0]][path[1]] = None
        for field in ("causal_relations", "chains"):
            if field in out:
                out[field] = [v for v in out[field] if v is not None]
    except ImportError:
        pass
    return out
