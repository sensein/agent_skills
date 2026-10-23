#!/usr/bin/env bash
# Headless test of examples/claude_code_demo.md: a fresh directory, the skill installed at project scope,
# the four prompts sent in ONE continued Claude Code session (as a person would type them), each timed.
# Usage: bash test_claude_code_demo.sh <empty work dir> [model]   -> writes <dir>/transcript.md
set -uo pipefail
WORK="$1"; MODEL="${2:-sonnet}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
mkdir -p "$WORK" && cd "$WORK"
python3 "$REPO/scripts/install_skills.py" --agent claude --scope project --skills neuroimaging-kg >/dev/null
PROMPTS=(
  "Use the neuroimaging-kg skill to build a knowledge graph for ABIDE I site Caltech — the phenotype table plus ReproNim's NIDM file with brain volumes — validate it, and load it into Oxigraph. Tell me in plain words what is in the graph."
  "How many Caltech participants have both a full-scale IQ (FIQ) score and a FreeSurfer hippocampal volume, by diagnostic group? Show the SPARQL query you used and explain it briefly."
  "Trace participant 51456's left-hippocampus volume from FreeSurfer back to the scan and the software it came from. Say which links are recorded in the graph and which are only inferred."
  "If we tightened the head-motion rule to mean framewise displacement below 0.15 mm, which participants would be excluded, and which results in our graph depend on them?"
)
OUT="$WORK/transcript.md"
echo "# Headless test of claude_code_demo.md ($(date -u +%Y-%m-%dT%H:%MZ), model: $MODEL)" > "$OUT"
for i in "${!PROMPTS[@]}"; do
  P="${PROMPTS[$i]}"
  CONT=(); [ "$i" -gt 0 ] && CONT=(--continue)
  START=$(date +%s)
  RES=$(claude -p "$P" ${CONT[@]+"${CONT[@]}"} --model "$MODEL" --setting-sources project --strict-mcp-config \
        --permission-mode acceptEdits --allowedTools Bash Read Write Edit Glob Grep Skill \
        --max-budget-usd 4 --output-format json 2>/dev/null)
  SECS=$(( $(date +%s) - START ))
  python3 - "$RES" "$SECS" "$((i + 1))" "$P" >> "$OUT" <<'PY'
import json, sys
res, secs, n, prompt = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    r = json.loads(res)
except json.JSONDecodeError:
    r = {"result": res[-3000:], "num_turns": None, "total_cost_usd": None}
print(f"\n## Prompt {n} ({secs} s, {r.get('num_turns')} turns, ${(r.get('total_cost_usd') or 0):.2f})\n")
print(f"> {prompt}\n")
print((r.get("result") or "").strip())
PY
  echo "prompt $((i + 1)): ${SECS}s"
done
