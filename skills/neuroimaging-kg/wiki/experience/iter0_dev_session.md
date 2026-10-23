# Development session (building the v0 skill and the one-site demo) — raw experience

Source: the author agent's own build of scripts/ and examples/abide_one_site, before any rollout.
Trimmed to the events that changed code or would change what an agent does.

| # | What was tried | What happened | Fix applied (in v0 code) |
|---|---|---|---|
| 1 | `join_identity --check AGE=<predicate>` on ReproNim's site NIDM | NIDM stores each variable under a per-file element IRI (`niiri:AGE_AT_SCAN_x7…`), so a fixed predicate matches nothing | checks accept `about:<concept IRI>` and `var:<sourceVariable>` |
| 2 | Same check, found no values | the conflict list was empty and looked like agreement | report `checks_compared` per check; warn when 0 |
| 3 | `about:<ncit:Diagnosis>` for diagnosis | two elements carry that concept (DSM_IV_TR and DX_GROUP) | use `var:DX_GROUP` for diagnosis |
| 4 | Query volumes from the site NIDM alone | no `rdfs:label`/region/unit on the measures: definitions live in three tool CDE files from three repos (ants_seg_to_nidm, fsl_seg_to_nidm, simple2/cde) | fetch and load the CDE files; query with the default graph as union |
| 5 | Filter hippocampus by `nidm:datumType ilx_0738276` | ANTs gives hippocampal *surface area* (mm^2) the same datum type as volume; FreeSurfer also tags voxel counts with the concept | select volume by `nidm:hasUnit "mm^3"` |
| 6 | Compare concept IRIs across files | InterLex appears as both `http://uri.interlex.org/ilx_…` and `http://uri.interlex.org/base/ilx_…` | match the form each file uses (age uses the short form, sex/FIQ the `base/` form in these files) |
| 7 | Count FIQ | ABIDE codes missing as -9999; csv2nidm writes NaN floats | filter `> 0` in SPARQL, drop -9999 in Python |
| 8 | Validate csv2nidm + ReproNim NIDM with the core shapes | 121 violations: NIDM sessions have no agent, tool outputs have no wasDerivedFrom, software agents carry versions under NIDM's own property | core shapes apply to authored layers only; third-party NIDM gets LinkML-generated shapes with --warn-only |
| 9 | Mutation test on a non-conforming baseline | reported "caught" although the baseline already failed | caught requires a conforming baseline |
| 10 | Validate csv2nidm output against PyNIDM's own LinkML schema | 176 results (multi-valued `choices`, dateTime typing) | record as findings, not a build gate |
| 11 | csv2nidm run | succeeds with "AnnexRepo" warnings outside a git-annex repo; instance IRIs are random UUIDs, so the output hash differs every run | ignore the warning; do not pin csv2nidm output by hash |
| 12 | Three-tool comparison for Caltech | the site file has FreeSurfer and FSL outputs only (no ANTs) | say "every tool that has one" |
| 13 | Gold answers | a gold computed with the same SPARQL as the answer only tests the store against itself | compute gold from the CSV/raw text, or by an rdflib walk, never by the same query |
