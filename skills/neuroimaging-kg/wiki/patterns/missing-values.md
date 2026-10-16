# Missing values: -9999 in ABIDE tables, NaN after csv2nidm

**Type:** pitfall · **Evidence:** experience/iter0_dev_session.md item 7; demo Q3 (participant 51473, FIQ -9999).

Filter `> 0` (or not NaN) in SPARQL and drop -9999 in pandas/csv before counting availability.
