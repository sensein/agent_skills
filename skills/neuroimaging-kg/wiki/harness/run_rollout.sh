#!/usr/bin/env bash
# One WikiSkill rollout: fresh directory, skill installed at project scope WITHOUT its wiki (the
# inference agent must not read the wiki, arXiv:2608.27454v1 §3), staged inputs, isolated headless
# Claude Code, then the deterministic checker.
# Usage: run_rollout.sh <iteration> <task_id> <work_root> <cache_dir> [model]
set -uo pipefail
ITER="$1"; TASK="$2"; WORK="$3"; CACHE="$4"; MODEL="${5:-sonnet}"
HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS/../../../.." && pwd)"
DIR="$WORK/iter$ITER/$TASK"; RAW="$WORK/raw/iter$ITER"; mkdir -p "$RAW"
rm -rf "$DIR" && mkdir -p "$DIR"
python3 "$REPO/scripts/install_skills.py" --dest "$DIR/.claude/skills" --skills neuroimaging-kg >/dev/null
rm -rf "$DIR/.claude/skills/neuroimaging-kg/wiki"
STAGE=$(python3 -c "import json,sys;t=json.load(open('$HARNESS/tasks.json'));s={**t['train'],**t['val']};print(' '.join(s['$TASK']['stage']))")
PROMPT=$(python3 -c "import json;t=json.load(open('$HARNESS/tasks.json'));s={**t['train'],**t['val']};print(s['$TASK']['prompt'])")
[ -n "$STAGE" ] && python3 "$HARNESS/stage.py" "$DIR" $STAGE
START=$(date +%s)
( cd "$DIR" && claude -p "$PROMPT" --model "$MODEL" --setting-sources project --strict-mcp-config \
    --no-session-persistence --permission-mode acceptEdits \
    --allowedTools Bash Read Write Edit Glob Grep Skill --max-budget-usd 4 \
    --output-format stream-json --verbose > "$RAW/$TASK.jsonl" 2> "$RAW/$TASK.stderr" )
ELAPSED=$(( $(date +%s) - START ))
RESULT=$(uv run -q "$HARNESS/check.py" "$TASK" "$DIR" --cache "$CACHE" 2>/dev/null || echo '{"task":"'$TASK'","pass":false,"details":{"error":"checker crashed"}}')
python3 - "$RESULT" "$ELAPSED" "$WORK/results_iter$ITER.jsonl" <<'PY'
import json, sys
r = json.loads(sys.argv[1]); r["seconds"] = int(sys.argv[2])
open(sys.argv[3], "a").write(json.dumps(r) + "\n")
print(f"{r['task']}: pass={r['pass']} ({r['seconds']} s)")
PY
