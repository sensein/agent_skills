# T1_sbl_fiq_hippocampus — PASS

- wall time: 43 s; turns: 14; cost: $0.21
- checker: `{"answer": {"1": 5, "2": 0}, "gold": {"1": 5, "2": 0}}`

## Commands (8)
1. `[Skill] neuroimaging-kg`
2. `find /private/tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus/.claude/skills/neuroimaging-kg/examples/abide_one_site -maxdepth 2`
3. `pwd && ls -la`
4. `cd /tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus && bash .claude/skills/neuroimaging-kg/examples/abide_one_site/run_demo.sh SBL`
5. `cd /tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus && python3 -c " import json g = json.load(open('graph/gold.json')) a = json.load(open('graph/answers.json')) print('GOLD Q3:', g.get('Q3_fiq_and_freesurfer_hippocampus')) print('AN`
6. `cd /tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus && python3 -c " import json a = json.load(open('graph/answers.json')) print(json.dumps(a, indent=2)[:2000]) " `
7. `[Write] /tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus/answer.json`
8. `cd /tmp/nkg-wikiskill-ee89ed94/iter0/T1_sbl_fiq_hippocampus && ls graph && cat answer.json`

## Tool errors (0)

## Final message (excerpt)

Built and validated the ABIDE I SBL knowledge graph in `./graph` (phenotype NIDM via `csv2nidm`, identity links to ReproNim's site NIDM, cohort/claim/provenance layers). Both the authored SHACL shapes passed with a caught mutation test, and the NIDM layer was checked against PyNIDM's own LinkML-generated shapes (findings recorded, not gating).

The SPARQL competency question (Q3) joining FIQ from the phenotype graph with FreeSurfer hippocampal volume from ReproNim's NIDM graph passed against gold answers computed independently from the source files:

- **DX_GROUP 1 (autism): 5 participants**
- **DX_GROUP 2 (control): 0 participants**

Written to `./answer.json` as `{"1": 5, "2": 0}`.
