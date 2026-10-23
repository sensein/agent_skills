# Where brain volumes live in ReproNim NIDM files

**Type:** knowledge gap → reference · **Evidence:** experience/iter0/T3_caltech_derived_layer.md (8 grep
calls, steps 4–11, to reverse-engineer the structure; 182 s, the slowest train task);
experience/iter0_dev_session.md items 4–6.

- Values: `?ent prov:wasGeneratedBy ?act ; ?measure ?value` in the **site** NIDM file.
- Who and which tool: `?act prov:qualifiedAssociation` has two associations — one whose agent carries
  `ndar:src_subject_id` (the participant) and one whose agent has `nidm:NIDM_0000164 <tool namespace>`
  (`fs:` FreeSurfer, `fsl:` FSL, `ants:` ANTs).
- What the measure means: **only** in the tool CDE files (`freesurfer_cde.ttl`, `fsl_cde.ttl`,
  `ants_cde.ttl`): `nidm:isAbout <UBERON region>`, `nidm:hasLaterality "Left"|"Right"`,
  `nidm:hasUnit "mm^3"`, `rdfs:label`. Load them alongside the site file (union default graph).
- Select volumes by `nidm:hasUnit "mm^3"`, not by datum type (ANTs gives surface area the volume
  datum type; FreeSurfer also tags voxel counts with the same region).
- Not every site has every tool (Caltech: FreeSurfer and FSL, no ANTs).
