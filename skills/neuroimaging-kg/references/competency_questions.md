# Competency question templates

A competency question is something the graph must answer, paired with a gold answer computed
independently of the graph (from the source table, a file hash, a paper). Write them before
building; keep each as a `.rq` file named after the question.

| Kind | Template | Gold source |
|---|---|---|
| Coverage | How many participants are in X, and how many link to Y? | row counts in the source tables |
| Cohort | How many participants pass rule R, by group? | the rule applied to the source table |
| Availability | How many have both variable A and measure B, by group? | source table ∩ independent traversal of the derived file |
| Provenance | Which step, software version, parameters and inputs (by hash) produced output O? | the build log and file hashes |
| Claim scope | Which claims use dataset D, of what type, on what population, with what evidence? | the recorded claim and its source |
| Impact | If rule R changes, which participants, folds and results depend on the change? | recompute with the new rule |
| Sharing | Which records may not leave the pond, and why? | consent scope and access level per record |
| Agreement | Do tools T1 and T2 give the same value for structure S (same unit)? | descriptive — report, do not gold |

Name variables in SELECT the way the gold JSON names them; compare rows as unordered sets.
