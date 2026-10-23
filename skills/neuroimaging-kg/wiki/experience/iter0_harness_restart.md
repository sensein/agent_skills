# Iteration 0, first attempt — harness failure, not a skill failure

- T2 (Stanford coverage) ran 40 s, 12 turns, $0.17. The agent loaded the skill, ran
  `examples/abide_one_site/run_demo.sh Stanford`, read `graph/join_report.json` and had the correct
  answer (40 / 40 / 0), but both writes of `answer.json` were refused: "Claude requested permissions
  to edit …/answer.json which is a sensitive file." T1 failed the same way.
- Cause: the rollout directories lived under `~/.claude/jobs/…`; Claude Code treats any path under
  `~/.claude` as sensitive and asks for approval, which a headless run cannot give.
- Fix: the run was stopped and restarted with the work root at `/tmp/nkg-wikiskill-ee89ed94`. These two
  rollouts are discarded and not scored.
- Kept as a harness note: headless rollouts must run outside `~/.claude`. Not a skill pattern.
- Observed strategy worth keeping: the agent reused the demo's `run_demo.sh <Site>` for a new site
  instead of rebuilding by hand.
