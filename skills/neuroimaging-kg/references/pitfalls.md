# Pitfalls (baseline v0)

- **Shared ID ≠ same person.** Record identity as a reviewable link with its rule and reviewer; check
  age/sex/diagnosis across sources and review conflicts.
- **Same term, different measure.** Two tools can tag different quantities with the same anatomy term.
  Check unit and definition before comparing values.
- **Missing values.** Source tables use codes such as -9999; converted graphs may hold NaN. Filter
  them before counting.
- **Cohort reconstructibility.** Record the rule that defined a cohort and the exact members, not just N.
- **Leakage-relevant provenance.** Record where feature selection, harmonisation and tuning happened
  relative to the cross-validation split.
- **Consent wording.** Use broad-sharing / study-only / named-uses. Never model "no consent".
- **AI-agent attribution.** Record AI agents as agents with their model id; do not attribute their
  output to a person without review.
