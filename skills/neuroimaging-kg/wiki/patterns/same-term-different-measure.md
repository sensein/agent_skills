# Same anatomy term, different measure

**Type:** pitfall · **Evidence:** experience/iter0_dev_session.md items 5–6; ABIDE package (CSV tagged
with UBERON:0001359 means total CSF in FSL but ventricles in ANTs/FreeSurfer, ~200× apart).

A shared concept IRI makes values joinable, not comparable. Before comparing across tools, match
unit (`nidm:hasUnit`), measure type and definition; InterLex IRIs appear in two forms
(`http://uri.interlex.org/ilx_…` and `…/base/ilx_…`) — match the form each file uses.
