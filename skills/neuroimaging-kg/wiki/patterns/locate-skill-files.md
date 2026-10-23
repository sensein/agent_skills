# Locate the skill's own files before searching the disk

**Type:** failure mode → workaround · **Evidence:** experience/iter0/T4_caltech_add_claim.md (step 3:
`find /` over the whole filesystem for `core_shapes.ttl`), T6 (`find` over the task directory).

The skill's scripts, shapes and examples sit next to `SKILL.md`. In a project install that is
`./.claude/skills/neuroimaging-kg/`; in a user install `~/.claude/skills/neuroimaging-kg/`. Set
`SKILL_DIR` once and use `$SKILL_DIR/scripts/…`, `$SKILL_DIR/assets/core_shapes.ttl`,
`$SKILL_DIR/examples/abide_one_site/…`. Never search `/`.
