# Skill impact (WikiSkill `skill-impact.md`)

| Iter | Proposal | Target | Diff summary | Motivating patterns | D_val score | R_best before | Outcome |
|---|---|---|---|---|---|---|---|
| 0 | baseline | neuroimaging-kg | — (commit a329df1) | — | 4/6 (median 106.5 s) | — | baseline |
| 1 | P1 | neuroimaging-kg | +SKILL.md "Before you start" (5 rules), +scripts/nidm_volumes.py (+2 tests), +2 modeling snippets, +PURPOSE.md (commit eee8ff0) | locate-skill-files, python-environment, reuse-demo-for-pinned-sites, persistent-identifiers, nidm-volume-structure, write-turtle-safely, same-term-different-measure | 5/6 (median 78.5 s) | 4/6 | **accepted** |

Per-run scores: `harness/val_scores/iter0*.jsonl` (baseline), `iter1c.jsonl` and `iter1d.jsonl` (candidate).
