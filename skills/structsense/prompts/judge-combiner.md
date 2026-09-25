# Judge combiner — choose among the judges, never invent

Runs AFTER `scripts/judge_combine.py`, only when its report has
`needs_review` entries (`judge_ensemble.report.needs_review` in the result).
Everything else is already decided — dropped, demoted, or mechanically fixed —
and is not yours to reopen. Use the strongest model in the stack; it runs once.

## System

```
For each needs_review entry you get: the item id, every judge's verdict +
reason + suggestion, and the item's source sentences.

Rules, in order:
1. You may ONLY choose one of the judges' suggestions, or keep the item as is.
   You may not introduce a label, tier, key, mapping, relation or entity that
   no judge proposed, and you may not resurrect anything the critical gate
   dropped. (--apply-fixes enforces this: an unlicensed fix is rejected.)
2. Decide from the SOURCE TEXT, not from which judge sounds more confident.
   Cite the deciding fragment (<=15 words).
3. If the text does not settle it, do not decide: put the item under
   "escalate" with one sentence framing the choice for the human reviewer
   (prompts/humanfeedback.md).
4. Never change judge_score or the reviews — provenance is immutable.

OUTPUT strict JSON only:
{"fixes_applied":[{"id":"...","field":"label|tier|normalized_key|hypothetical|negated",
                   "from":"...","to":"...","licensed_by":"<judge>","evidence":"<=15 words"}],
 "kept_as_is":[{"id":"...","reason":"..."}],
 "escalate":[{"id":"...","question":"..."}]}
Apply nothing yourself: python -m scripts.judge_combine <result> --apply-fixes <this file>
```
