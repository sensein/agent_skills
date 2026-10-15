# neuroimaging-kg wiki — persistent knowledge behind the skill

This directory follows **WikiSkill** (Tang, Rashtchian, Ferng, Tomkins, Juan and Vu, *WikiSkill:
Compiling Agent Experience into Persistent Knowledge for Skill Evolution*, arXiv:2608.27454v1, 27 Aug
2026, https://arxiv.org/html/2608.27454v1): agent experience is kept in three layers, and skill
updates are built from the middle one and accepted only if they help on held-out tasks.

| Paper layer | Here | Contents |
|---|---|---|
| raw (immutable execution traces) | `experience/` | one trimmed note per rollout: outcome, time, cost, every command, every tool error, final message. Full stream-json traces are kept outside the repo. |
| wiki (persistent knowledge) | `patterns/`, `evolution_log.md`, `skill-impact.md` | `patterns/*.md`: one failure mode or successful strategy each, with the workaround and links to the experience notes that support it. `evolution_log.md` (the paper's `logs.md`): what each iteration did. `skill-impact.md`: each proposed skill change, its diff summary, validation score and whether it was kept. |
| skills | `../SKILL.md`, `../PURPOSE.md`, `../scripts/`, `../references/` | the executable skill; `PURPOSE.md` links each part of the skill to the patterns that motivated it. |

`harness/` holds the task splits (`tasks.json`), the staging of inputs (`stage.py`), the deterministic
checker (`check.py`), the rollout runner (`run_rollout.sh`, isolated headless Claude Code with the
skill installed **without this wiki**) and the trace trimmer (`trim_trace.py`).

## For agents using the skill

Before a non-trivial build, read `patterns/` (an index is at the top of `evolution_log.md`). After a
build that hit a new problem, append a note to `experience/` (what you tried, what happened, the fix)
and, if it generalises, a pattern page — the next skill update is proposed from these pages, not from
memory. Never delete a pattern because a skill change was rejected: the wiki is not rolled back.

## Loop as run here (see evolution_log.md for the record)

1. Inference: run the current skill on every training task (`harness/tasks.json` → `train`) with the
   wiki hidden from the agent; trim each trace into `experience/`.
2. Wiki maintenance: root-cause the failures, extract strategies from the passes, update `patterns/`.
3. Skill proposal: one change set to SKILL.md / references / scripts, built from named patterns.
4. Gate: run the candidate on the held-out validation tasks (`val`); keep it only if the validation
   pass rate is strictly higher than the best so far (the paper's rule), otherwise revert the skill.
   The wiki keeps everything either way. The Claude Code demo prompts (`../examples/claude_code_demo.md`)
   are the untouched test set, run once at the end.
