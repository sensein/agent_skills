# Evolution log (WikiSkill `logs.md`)

Method: WikiSkill — Tang, Rashtchian, Ferng, Tomkins, Juan and Vu, *WikiSkill: Compiling Agent Experience
into Persistent Knowledge for Skill Evolution*, arXiv:2608.27454v1 (27 Aug 2026),
https://arxiv.org/html/2608.27454v1. Algorithm 1 per iteration k: roll out the current skill S(k−1) on
D_train (inference agent has no wiki access) → Wiki Maintainer consolidates traces into the wiki →
Skill Proposer writes one patch from the wiki → candidate evaluated on D_val → accept iff
R(val) > R_best, else revert the skill; the wiki is never rolled back.

## Pattern index

| Pattern | Kind | Evidence |
|---|---|---|
| [locate-skill-files](patterns/locate-skill-files.md) | failure → workaround | T4, T6 |
| [reuse-demo-for-pinned-sites](patterns/reuse-demo-for-pinned-sites.md) | strategy | T1, T2, T5 |
| [nidm-volume-structure](patterns/nidm-volume-structure.md) | knowledge gap | T3; dev session |
| [python-environment](patterns/python-environment.md) | failure → workaround | T3 |
| [persistent-identifiers](patterns/persistent-identifiers.md) | failure → rule | T4 (the train failure) |
| [write-turtle-safely](patterns/write-turtle-safely.md) | failure → workaround | T6 |
| [same-term-different-measure](patterns/same-term-different-measure.md) | pitfall | dev session; ABIDE package |
| [checks-must-compare-something](patterns/checks-must-compare-something.md) | pitfall | dev session |
| [missing-values](patterns/missing-values.md) | pitfall | dev session; demo Q3 |

## Setup

- Tasks: `harness/tasks.json` — D_train = T1–T6 (build + query SBL; Stanford coverage join; derived
  hippocampal layer with provenance; add a scoped published claim; join a lab table with planted
  defects; pond→lake egress gate). D_val = V1–V3 (different site and query, OHSU; tool
  disagreement, Caltech; malformed table with BOM / whitespace / duplicate). D_test = the Claude Code
  demo prompts (`examples/claude_code_demo.md`), run once at the end.
- R = pass rate from `harness/check.py` (gold computed from pinned source files and independent rdflib
  walks, never from the agent's graph). Time recorded as a secondary metric.
- Inference agent: Claude Code `claude -p` (model `sonnet`), `--setting-sources project`
  (no user skills or memories), `--strict-mcp-config`, skill installed at project scope **without**
  `wiki/`, $4 budget cap per rollout. Maintainer and proposer: the author agent (Claude Opus 5.5).

## Iteration 0 — baseline S0 (commit a329df1)

- First attempt discarded: rollouts were placed under `~/.claude/…`, which Claude Code treats as
  sensitive, so headless writes were refused (experience/iter0_harness_restart.md). Rerun from `/tmp`.
- D_train: 5/6 pass (T4 fails). Times 28–182 s, $0.12–0.79 per task.
- D_val (2 replicates): **4/6**, median 106.5 s. V2 failed both times (the agent wrote the count of
  disagreeing participants into `n_with_both`).
- Wiki maintenance: 9 pattern pages from the six training traces and the development session.
  Validation traces were **not** read into the wiki (only their scores are stored, `harness/val_scores/`).

## Iteration 1 — candidate S1 (commit eee8ff0)

- Proposal (one patch, from patterns): SKILL.md "Before you start" (locate files; uv; reuse
  run_demo.sh; persistent identifiers; consult the wiki); `scripts/nidm_volumes.py` + tests;
  modeling snippets for NIDM volumes and a pond record with a promotion attempt; `PURPOSE.md`.
- D_val (2 replicates; a first pair was cut off by an API session limit, HTTP 429, and is not
  scored): **5/6**, median 78.5 s (V1 2/2, V2 1/2, V3 2/2).
- Gate: 5/6 > 4/6 → **accepted** (also faster: median −28 s). Caution: the gain is a single V2 run
  out of two and could be noise; no pattern targeted V2's error directly.
- Stopped after one evolution iteration (budget bound); no iteration-2 training rollouts were run.

## Deviations from the paper, stated plainly

1. One evolution iteration (K = 1) instead of several; 6 training and 3 validation tasks.
2. Two replicates per validation task to reduce single-run noise (the paper scores one rollout set).
3. The Wiki Maintainer and Skill Proposer were the same author agent working by hand from trimmed
   traces, not separate prompted agents with the paper's Appendix E prompts.
4. Traces are trimmed (commands, errors, final message) rather than kept complete in the repo; the
   full stream-json traces stayed outside the repository and were deleted after summarising.
5. The development session's own debugging notes (experience/iter0_dev_session.md) were also
   compiled into the wiki — the paper's wiki is built only from rollout traces.
6. Gate on pass rate as in the paper (strictly greater); time recorded but not used to decide.

## Open items (observed, not acted on)

- A UTF-8 byte-order mark in a CSV header makes `join_identity.py` raise `KeyError` on the ID column
  (seen in a **validation** trace; agents recovered by re-reading with `utf-8-sig`). Not fixed here,
  because using validation traces to change the skill would leak D_val; add a training task with a
  BOM in the next iteration.
- V2-style answer-contract errors (reporting one count in another field) have no training evidence yet.
