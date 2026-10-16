# Reuse the demo pipeline for any pinned ABIDE site

**Type:** successful strategy · **Evidence:** experience/iter0/T1 (43 s), T2 (28 s), T5; iter0_harness_restart.md.

`bash $SKILL_DIR/examples/abide_one_site/run_demo.sh <Site>` works for every site listed in
`pins.json` (Caltech, SBL, OHSU, Stanford), not only Caltech. It fetches and hash-checks inputs, builds
all layers, validates and writes `graph/join_report.json` and `graph/answers.json`. The fastest passing
rollouts started here and then queried or read the outputs; the slow ones rebuilt pieces by hand.
For a new site, add its pinned NIDM URL and SHA-256 to `pins.json` first.
