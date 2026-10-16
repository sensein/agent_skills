# Identify papers, datasets and people by persistent identifiers

**Type:** failure mode → rule · **Evidence:** experience/iter0/T4_caltech_add_claim.md — the graph was
right, but the answer named papers by citation strings ("Di Martino et al. 2014, Molecular
Psychiatry: …") instead of DOIs, so it could not be joined to anything.

In the graph and in answers, a paper is its DOI IRI (`https://doi.org/10.1038/mp.2013.78`), a person
an ORCID, a tool a repository URL or RRID, a dataset its landing page or accession. Labels are extra
`rdfs:label` triples. When asked for "the paper", return the DOI (add the label only if asked).
