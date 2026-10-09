"""Human feedback: the optional correction loop after the judge, for every mode.

    extract -> concept map -> judge -> HUMAN FEEDBACK -> output
                                         ^                 |
                                         +-- output loop --+   (optional: revise again)

A reviewer corrects through the same operations a judge uses (scripts/review_loop.py:
drop / restore / set / remap / demote / approve / note), so a human edit is held to the
same rules: allowlisted fields, ids only from a mapping tool (the trusted ontology files
first), nothing added that the paper did not state. Every edit is logged with the
reviewer, the value before and after, and why.

    # 1. what needs a human: escalations, contested items, drops, low scores, new remaps
    python -m scripts.human_feedback queue --mode ner work/paper_final.json -o feedback.json
    # 2. the reviewer (or the agent turning their words into ops, prompts/humanfeedback.md)
    #    fills feedback.json "ops": [{"id": "...", "action": "set", "field": "label", "value": "CellType"}]
    # 3. apply, then re-render the output (NER: --ttl to rewrite and re-gate the Turtle)
    python -m scripts.human_feedback apply --mode ner work/paper_final.json --feedback feedback.json --ttl paper.ttl
    # or the interactive loop (approve / abort / edit / skip, 60 s timeout -> skip)
    python -m scripts.human_feedback interactive --mode abcd out/paper_abcd.json
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = _SCRIPTS_DIR.parent
for _p in (str(_SCRIPTS_DIR), str(SKILL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_loop as rl  # noqa: E402

DEFAULT_THRESHOLD = 0.7


def _escalated(mode: str, data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    if mode == "ner":
        block = data.get("judge_ensemble") or {}
        for e in (block.get("combiner") or {}).get("escalated") or []:
            out[str(e.get("id"))] = e.get("question") or "escalated by the combiner"
        if not block.get("combiner"):
            for n in (block.get("report") or {}).get("needs_review") or []:
                out[str(n.get("id"))] = n.get("conflict") or "judges disagree"
    loop = data.get("review_loop") or {}
    for e in loop.get("escalated") or []:
        out[str(e.get("id"))] = e.get("question") or "escalated"
    if mode != "ner" and not loop.get("escalated"):
        for n in (loop.get("judge") or {}).get("needs_review") or []:
            out[str(n.get("id"))] = n.get("conflict") or "judges disagree"
    return out


def _view(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "kind", "surface", "fields", "mapping", "evidence", "judge_score")
            if r.get(k) not in (None, {}, [])}


def build_queue(mode: str, data: dict, *, threshold: float = DEFAULT_THRESHOLD, everything: bool = False) -> list[dict]:
    """Items a human should look at, most urgent first. `everything` lists all items."""
    recs = rl.records(mode, data)
    esc = _escalated(mode, data)
    scores = (data.get("review_loop") or {}).get("scores") or {}
    reviewed = {}
    for r in ((data.get("review_loop") or {}).get("rounds") or []):
        if r.get("actor") == "human":
            for op in r.get("ops") or []:
                if op.get("applied"):
                    reviewed[str(op.get("id")).lower()] = True
    q = []
    for r in recs:
        why = []
        if r["id"] in esc:
            why.append(f"escalated: {esc[r['id']]}")
        s = r.get("judge_score") if mode == "ner" else (scores.get(r["id"]) or {}).get("judge_score")
        if isinstance(s, (int, float)) and s < threshold:
            why.append(f"low judge score {s}")
        ref = r["ref"]
        method = (ref["items"][0] if mode == "ner" else ref).get("alignment_method") if isinstance(ref, dict) else None
        if method in ("judge_remapped", "combiner_remapped"):
            why.append("mapping re-run from a judge's query; not yet reviewed")
        if mode == "ait" and r["kind"] == "mapping" and ref.get("mapped_by") == "llm" and ref.get("supersedes_mapping_id"):
            why.append("edge revised by a judge")
        if (why or everything) and not reviewed.get(str(r["id"]).lower()):
            q.append({**_view(r), "why": why or ["listed (--all)"], "score": s})
    q.sort(key=lambda x: (not any(w.startswith("escalated") for w in x["why"]),
                          x["score"] if isinstance(x.get("score"), (int, float)) else 1.0))
    for iid, d in ((data.get("review_loop") or {}).get("dropped") or {}).items():
        items = d.get("items") or [d.get("item") or d.get("row") or {}]
        first = items[0]
        ev = first.get("evidence") if isinstance(first.get("evidence"), dict) else {}
        surface = next((first.get(k) for k in ("entity", "term", "mention_as_written", "construct", "statement",
                                                "label_verbatim", "cell_type_name_as_in_paper") if first.get(k)), None)
        q.append({"id": iid, "kind": "dropped", "surface": surface,
                  "why": [f"dropped by {d.get('by') or 'a gate'}; `restore` to undo"],
                  "evidence": {"sentence": first.get("sentence") or ev.get("quote") or first.get("evidence_sentence")}})
    return q


def template(mode: str, target: Path, data: dict, **kw) -> dict:
    return {
        "mode": mode, "target": str(target),
        "instructions": ("Add operations under `ops`, one per correction, then run "
                         "`python -m scripts.human_feedback apply`. Actions: drop | restore | set | remap | demote | "
                         "approve | note. `set` takes field + value from `fixable`; `remap` takes value = a search "
                         "term (the tool finds the id; an AIT edge takes {ait_node_id, ait_cell_type_label, "
                         "skos_relation}); `restore` undoes a drop listed below. New items are refused: re-run "
                         "extraction for those."),
        "fixable": {k: {f: (list(v) if isinstance(v, (list, tuple)) else v) for f, v in fs.items()}
                    for k, fs in rl.FIXABLE[mode].items()},
        "queue": build_queue(mode, data, **kw),
        "ops": [],
    }


def apply_feedback(mode: str, data: dict, ops: list[dict], *, by: Optional[str] = None,
                   tools: Optional[rl.Tools] = None) -> list[dict]:
    log = rl.apply_ops(mode, data, ops, actor="human", by=by or os.environ.get("USER") or "human",
                       tools=tools, round_name=None)
    data.setdefault("human_feedback_applied", True)
    return log


def ops_from_text(mode: str, data: dict, text: str, *, call: Callable[..., str], model: str) -> dict:
    """Framework mode only: turn a reviewer's words into ops (prompts/humanfeedback.md).
    In host-model mode the agent does this itself and writes the ops file."""
    from json_repair import parse_or_repair
    from judge_ensemble import system_prompt
    q = build_queue(mode, data, everything=True)[:400]
    user = json.dumps({"mode": mode, "fixable": template(mode, Path("."), data)["fixable"],
                       "items": q, "feedback": text}, ensure_ascii=False, default=str)
    raw = call(model=model, system=system_prompt("prompts/humanfeedback.md"), user=user, json_mode=True,
               temperature=0)
    return parse_or_repair(raw) or {"ops": [], "errors": [{"code": "unparseable", "feedback": text}]}


# --------------------------------------------------------------------------- #
# interactive loop (approve / abort / edit / skip), with the optional output loop
# --------------------------------------------------------------------------- #

def _ask(prompt: str, timeout: Optional[float]) -> Optional[str]:
    print(prompt, end="", file=sys.stderr, flush=True)
    if not sys.stdin.isatty():
        return None
    if timeout:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            print("\n(timeout: skipping feedback)", file=sys.stderr)
            return None
    return sys.stdin.readline().strip()


def _edit(doc: dict) -> Optional[dict]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, default=str)
        path = fh.name
    try:
        subprocess.call([editor, path])
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        print(f"could not read the edited feedback: {e}", file=sys.stderr)
        return None
    finally:
        Path(path).unlink(missing_ok=True)


def interactive(mode: str, data: dict, target: Path, *, render: Optional[Callable[[dict], str]] = None,
                timeout: Optional[float] = 60.0, tools: Optional[rl.Tools] = None,
                text_to_ops: Optional[Callable[[str], dict]] = None, output_loop: bool = True) -> str:
    """Returns "approved" | "aborted" | "skipped". `render(data)` writes the output
    and returns a one-line summary (e.g. the TTL gate verdict); with output_loop the
    reviewer sees it and may go round again."""
    while True:
        tpl = template(mode, target, data)
        print(f"\n== human feedback ({mode}): {len(tpl['queue'])} item(s) need a look ==", file=sys.stderr)
        for it in tpl["queue"][:15]:
            print(f"  {it['id']}  [{it.get('kind')}] {str(it.get('surface'))[:60]!r}  — {'; '.join(it['why'])[:120]}",
                  file=sys.stderr)
        choice = _ask("1) approve and continue  2) abort  3) edit (ops in $EDITOR)  "
                      "4) skip  5) type feedback  > ", timeout)
        if choice in (None, "", "4"):
            data.setdefault("review_loop", {}).setdefault("rounds", []).append(
                {"actor": "human", "at": rl.utc_now(), "skipped": True, "ops": []})
            return "skipped"
        if choice == "2":
            return "aborted"
        if choice == "1":
            data.setdefault("review_loop", {}).setdefault("rounds", []).append(
                {"actor": "human", "at": rl.utc_now(), "approved": True, "ops": []})
            return "approved"
        if choice == "3":
            edited = _edit(tpl)
            ops = (edited or {}).get("ops") or []
        elif choice == "5" and text_to_ops:
            text = _ask("feedback> ", None) or ""
            spec = text_to_ops(text) if text else {}
            ops = spec.get("ops") or []
            for err in spec.get("errors") or []:
                print(f"  not applied: {err}", file=sys.stderr)
        else:
            print("  (option 5 needs a model; write ops with option 3)", file=sys.stderr)
            continue
        if not ops:
            continue
        log = apply_feedback(mode, data, ops, tools=tools)
        for e in log:
            print(("  applied " if e["applied"] else "  REJECTED ") + f"{e.get('action')} {e.get('id')}"
                  + ("" if e["applied"] else f": {e['rejected_because']}"), file=sys.stderr)
        if render is not None:
            print(f"  output: {render(data)}", file=sys.stderr)
        if not output_loop:
            return "approved"


# --------------------------------------------------------------------------- #
# NER output rendering (JSON -> TTL -> gate), shared with the pipeline
# --------------------------------------------------------------------------- #

def render_ner_ttl(result: dict, ttl_path: Path, *, kg_plan: Optional[dict] = None,
                   source_path: Optional[Path] = None) -> dict:
    from json_to_ttl import result_to_ttl
    from validate_ttl import validate_file
    ttl, conv = result_to_ttl(result, kg_plan=kg_plan, source_path=source_path)
    ttl_path.write_text(ttl)
    gate = validate_file(ttl_path)
    return {"path": str(ttl_path), "triples": conv.get("triples"), "ok": gate["ok"],
            "violations": gate.get("violation_count"), "warnings": gate.get("warning_count")}


def _render_for(mode: str, target: Path, ttl: Optional[Path], kg_plan: Optional[dict]) -> Callable[[dict], str]:
    def render(data: dict) -> str:
        if mode == "ner" and ttl:
            g = render_ner_ttl(data, ttl, kg_plan=kg_plan)
            rl.save(mode, data, target)
            return f"{g['path']}: {g['triples']} triples, gate {'VALID' if g['ok'] else 'FAILED'} " \
                   f"({g['violations']} violations)"
        paths = rl.save(mode, data, target)
        if mode == "ait":
            from scripts import ait_tables
            issues = ait_tables.validate(target)
            errs = [i for i in issues if i.level == "error"]
            return f"{len(paths)} file(s); ait_tables validate: {len(errs)} error(s)"
        return ", ".join(p.name for p in paths)
    return render


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("queue", "apply", "interactive"):
        p = sub.add_parser(name)
        p.add_argument("--mode", choices=rl.MODES, required=True)
        p.add_argument("target", type=Path, help="NER result JSON, <stem>_abcd.json, or the AIT output dir")
        p.add_argument("--ttl", type=Path, help="NER: rewrite and re-gate this Turtle after applying")
        p.add_argument("--kg-plan", type=Path, help="NER: kg_plan.json for the Turtle")
        p.add_argument("--offline", action="store_true", help="remaps use the trusted ontology files only")
        if name == "queue":
            p.add_argument("-o", "--output", type=Path, default=None, help="default: <target dir>/feedback.json")
            p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
            p.add_argument("--all", action="store_true", help="list every item, not only those needing a look")
        elif name == "apply":
            p.add_argument("--feedback", type=Path, required=True, help="feedback.json with `ops`")
            p.add_argument("--by", default=None, help="reviewer name for the log")
        else:
            p.add_argument("--timeout", type=float, default=60.0, help="seconds before 'skip' (0 = wait)")
            p.add_argument("--no-output-loop", action="store_true")
    a = ap.parse_args()
    data = rl.load(a.mode, a.target)
    plan = json.loads(a.kg_plan.read_text()) if a.kg_plan else None
    tools = rl.Tools(offline=a.offline)
    if a.cmd == "queue":
        if a.output:
            out = a.output
        elif a.mode == "abcd":  # where abcd_extract looks on its next run
            out = a.target.parent / "feedback" / f"{a.target.name.removesuffix('.json').removesuffix('_abcd')}.feedback.json"
            out.parent.mkdir(parents=True, exist_ok=True)
        else:
            out = (a.target if a.mode == "ait" else a.target.parent) / "feedback.json"
        tpl = template(a.mode, a.target, data, threshold=a.threshold, everything=a.all)
        out.write_text(json.dumps(tpl, indent=1, ensure_ascii=False, default=str) + "\n")
        print(f"{len(tpl['queue'])} item(s) queued -> {out}", file=sys.stderr)
        return 0
    render = _render_for(a.mode, a.target, a.ttl, plan)
    if a.cmd == "apply":
        spec = json.loads(a.feedback.read_text())
        ops = (spec.get("ops") if isinstance(spec, dict) else spec) or []
        log = apply_feedback(a.mode, data, ops, by=a.by, tools=tools)
        for e in log:
            print(("applied " if e["applied"] else "REJECTED ") + f"{e.get('action')} {e.get('id')}"
                  + ("" if e["applied"] else f": {e['rejected_because']}"), file=sys.stderr)
        print(render(data), file=sys.stderr)
        return 0 if all(e["applied"] for e in log) else 1
    status = interactive(a.mode, data, a.target, render=render, timeout=a.timeout or None, tools=tools,
                         output_loop=not a.no_output_loop)
    if status != "aborted":
        print(render(data), file=sys.stderr)
    print(f"human feedback: {status}", file=sys.stderr)
    return 1 if status == "aborted" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
