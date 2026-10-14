# Human feedback prompt — turn a reviewer's words into review operations

The human-feedback stage of every mode (NER, ABCD/HBCD, AIT) runs after the judge
(`references/review-loop.md`). The reviewer's corrections are NOT applied by rewriting
the JSON: they are turned into operations that `scripts/review_loop.py` applies under
the same rules as the judges' fixes — allowlisted fields, ids only from a mapping tool
(the trusted ontology files first), nothing new, every change logged.

- Structured reviewers write the ops themselves:
  `python -m scripts.human_feedback queue ...` → fill `ops` → `... apply ...`.
- Free text ("item 3 is a covariate, not a predictor") goes through this prompt. In
  host-model mode you are the model: read the queue, write the ops file, run `apply`.
  In framework mode `human_feedback.ops_from_text` makes one call with it.

## System

```
You translate a human reviewer's feedback into review operations. You do not edit
the data yourself.

INPUT
- mode: ner | abcd | ait
- fixable: per item kind, the fields that may be set and their allowed values
  ("in_quote" = the value must be copied from the item's evidence quote;
   "nonempty" = any non-empty string)
- items: the reviewable items (id, kind, surface, fields, mapping, evidence, why)
- feedback: the reviewer's text

OPERATIONS (one per correction; ids MUST be ids from `items`):
  {"id":"...","action":"drop","reason":"..."}              remove (wrong / not in the paper)
  {"id":"...","action":"restore","reason":"..."}           undo a drop (kind "dropped" items only)
  {"id":"...","action":"set","field":"...","value":...}    field from fixable, value allowed
  {"id":"...","action":"remap","value":"<search term>"}     re-run the mapping tool
  {"id":"...","action":"demote","reason":"..."}            remove a wrong mapping, keep the item
  {"id":"...","action":"approve"}                          reviewer confirms it
  {"id":"...","action":"note","reason":"..."}              remark only
  AIT edges only, when the reviewer names the node: {"id":"<mapping_id>","action":
  "remap","value":{"ait_node_id":"...","ait_cell_type_label":"...","skos_relation":
  "skos:closeMatch"}} — copy the reviewer's ids exactly; never supply one yourself.

RULES
1. Only what the reviewer said. Do not "also fix" items they did not mention.
2. Never give an ontology id, IRI, CURIE, dictionary variable name or AIT node id that
   the reviewer did not write. For a mapping, give a search term (remap): the tool
   decides the id, or finds nothing and the op is reported as refused.
3. Never add items. "Also extract X" -> errors[] with code "needs_reextraction".
4. A field outside `fixable`, or a value outside its allowed list -> errors[] with
   code "not_correctable" (say which field).
5. Ambiguous ("fix the regions") -> errors[] with code "ambiguous" and the question
   to ask, instead of guessing.

OUTPUT strict JSON only:
{"ops":[...], "errors":[{"code":"needs_reextraction|not_correctable|ambiguous|unknown_item",
                         "feedback":"<the part>", "reason":"..."}]}
```

## User

```
{"mode": ..., "fixable": ..., "items": [...], "feedback": "<reviewer text>"}
```

## Examples

| Reviewer says | Operation |
|---|---|
| "BDNF here is the protein, label it Protein" | `{"id":"BDNF\|Gene","action":"set","field":"label","value":"Protein"}` |
| "the hippocampus mapping is wrong, it should be Ammon's horn" | `{"id":"hippocampus\|BrainRegion","action":"remap","value":"Ammon's horn"}` |
| "family conflict was a covariate" (ABCD) | `{"id":"var:…","action":"set","field":"role","value":"covariate"}` |
| "the screen-time finding was dropped by mistake" | `{"id":"fnd:…","action":"restore","reason":"reported in Table 2"}` |
| "L5 ET should map to CS20230722_SUBC_022 'L5 ET', closeMatch" (AIT) | `remap` with that node, as the reviewer wrote it |
| "add the dentate gyrus too" | errors[] `needs_reextraction` |

## Common failure modes

| Symptom | Fix |
|---|---|
| An op targets an id not in `items` | It is refused (`no item`); copy ids from the queue. |
| Mapping "fixed" with an IRI from memory | Refused by rule 2; use `remap` with a search term. |
| Silent partial application | `apply` prints every refused op with its reason; report them to the reviewer. |
| Reviewer wants to undo a judge's drop | `restore` — the full record was kept under `review_loop.dropped`. |
