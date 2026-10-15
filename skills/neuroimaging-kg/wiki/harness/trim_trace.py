"""Trim a stream-json rollout trace into a short experience note (the raw layer kept in the repo).

Usage: python trim_trace.py <trace.jsonl> <result.json-line> <out.md>
Keeps: outcome, time, turns, cost, every Bash command (first 240 chars), every tool error (first
400 chars), and the agent's final message (first 800 chars). Full traces stay outside the repo.
"""
import json
import sys

trace, result_line, out = sys.argv[1], sys.argv[2], sys.argv[3]
res = json.loads(result_line)
cmds, errors, final, meta = [], [], "", {}
for line in open(trace):
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get("type") == "assistant":
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                inp = c.get("input", {})
                if c.get("name") == "Bash":
                    cmds.append(inp.get("command", "")[:240].replace("\n", " "))
                elif c.get("name") in ("Write", "Edit"):
                    cmds.append(f"[{c['name']}] {inp.get('file_path', '')}")
                elif c.get("name") == "Skill":
                    cmds.append(f"[Skill] {inp.get('skill', inp)}")
    if ev.get("type") == "user":
        for c in ev.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                text = c.get("content")
                text = text if isinstance(text, str) else json.dumps(text)
                if c.get("is_error") or "Traceback" in text or "Error" in text[:400]:
                    errors.append(text[:400].replace("\n", " ⏎ "))
    if ev.get("type") == "result":
        final = (ev.get("result") or "")[:800]
        meta = {k: ev.get(k) for k in ("num_turns", "duration_ms", "total_cost_usd", "is_error")}
with open(out, "w") as fh:
    fh.write(f"# {res['task']} — {'PASS' if res['pass'] else 'FAIL'}\n\n")
    fh.write(f"- wall time: {res.get('seconds')} s; turns: {meta.get('num_turns')}; cost: ${meta.get('total_cost_usd') or 0:.2f}\n")
    fh.write(f"- checker: `{json.dumps(res['details'], default=str)[:700]}`\n\n## Commands ({len(cmds)})\n")
    fh.writelines(f"{i + 1}. `{c}`\n" for i, c in enumerate(cmds[:60]))
    fh.write(f"\n## Tool errors ({len(errors)})\n")
    fh.writelines(f"- {e}\n" for e in errors[:25])
    fh.write(f"\n## Final message (excerpt)\n\n{final}\n")
