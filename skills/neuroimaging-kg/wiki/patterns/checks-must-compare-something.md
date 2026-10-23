# A cross-graph check that compared nothing is not agreement

**Type:** pitfall · **Evidence:** experience/iter0_dev_session.md items 1–3.

NIDM stores each variable under a per-file element IRI; check by concept (`about:<IRI>`) or by source
variable (`var:<name>`), and read `checks_compared` in the join report — 0 means no values were found.
Diagnosis: two elements share `ncit:Diagnosis` (DSM_IV_TR, DX_GROUP); use `var:DX_GROUP`.
