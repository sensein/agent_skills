# The review loop: extract → concept map → judge → human feedback → output

Every mode — NER (general / neuroscience / cns-cells), ABCD/HBCD and AIT cell-type
mapping — goes through the same stages. Only the item shapes differ.

```
extract ──► concept map ──► judge panel ──► human feedback ──► output (+ gate)
            trusted files     gates, fixes,    optional;          TTL / JSON / MD / CSV
            → local → OLS     remaps; ties →   same ops as             │
            MCP → BioPortal   combiner →       the judges              │
                              escalations ─────►                       │
                                                 ▲   optional output loop
                                                 └────────────────────┘
```

Both the judge and the human **correct**: neither only scores. They correct through
one vocabulary of operations, applied by one module (`scripts/review_loop.py`), so a
human edit is held to the same rules as a judge's fix.

## 1. Concept map — trusted ontologies first

`concept_mapping.json` `sources_priority` (default):

1. **trusted** — the ontology files in `trusted_ontologes/`, in `priority.md` order
   (indexed once: `python -m scripts.concept_mapping index`). Only when none of them
   names the term does the item move on.
2. **local_hybrid** — the local mapper (`http://localhost:8000`), if it is running.
3. **ols** — OLS through its MCP server, `https://www.ebi.ac.uk/ols4/api/mcp`
   (`scripts/ols_mcp_map.py`, `remote.ols_mcp_url`). The server's `searchClasses`
   ranks loosely and returns no synonyms, so a hit counts only when a class label
   equals the query; anything weaker stays unmapped for the judge to look at.
4. **bioportal** — last; needs `BIOPORTAL_API_KEY`.

Every mode uses this cascade:

| Mode | What is mapped | Label route | Where it lands |
|---|---|---|---|
| NER | every entity / key term (`concept_mapping.map_result`) | the extractor label | `ontology_id`, `mapping_source`, … on each mention |
| ABCD | constructs, and findings' constructs (`review_loop.map_abcd`), alongside the Cognitive Atlas id the verifier attaches | `CognitiveConstruct` (COGAT, COGPO, NBO, MF, MFOEM, MFOMD, HP) | `ontology_id`, `ontology_mapping_source`, `ontology_match_tier`; TTL `skos:<tier>` |
| AIT | every non-quarantined entity: cell types, genes, species, regions (`review_loop.map_ait`) | CellType / Gene / Species / BrainRegion | `concept_mappings.csv` (fills `ncbi_taxon_id` when empty) |

`pipeline.py --mapper local|ols|bioportal` is now "trusted files, then that backend":
no backend skips the trusted files.

## 2. Judge — same panel for every mode

The panel and its gates are in `judges_config.json`. NER uses `judge_prepare.py` →
judges → `judge_combine.py`. ABCD/AIT use `record_judge.py prepare` → judges
(`prompts/judge-record.md`, one judge at a time) → `record_judge.py combine`.

| Verdict | Effect |
|---|---|
| critical fail (grounding_script, grounding, claims) | **drop**: NER mentions removed; ABCD item → `rejected[]`; AIT entity quarantined and its edges superseded with `none`. The full record is kept under `review_loop.dropped` so a human can `restore` it. |
| mapping fail | **demote** the mapping (the item stays). With `suggestion.query` — a better *search term* from the paper, never an id — the **mapping tool runs again** (trusted first) and its hit, if any, becomes the mapping (`judge_remapped`). The judge may also give a query for an *unmapped* NER item. |
| mapping flag + tier | **set** the tier (NER `mapping_tier`, ABCD `ontology_match_tier`, AIT a superseding edge row with the new `skos_relation`). |
| labeling / claims flag + suggestion | **set** an allowlisted field (NER label; ABCD role, respondent, construct_kind, direction, effect_size from the quote; AIT entity_type, hierarchy_level, …). |
| contested, or a fail with nothing usable | `needs_review` → combiner (chooses among judges' suggestions only) → otherwise **escalated to the human queue**. |

## 3. Human feedback — optional, same operations

```
python -m scripts.human_feedback queue --mode {ner|abcd|ait} <target>    # what needs a look
#   fill "ops" in the queue file (or say it in words: prompts/humanfeedback.md turns words into ops)
python -m scripts.human_feedback apply --mode … <target> --feedback <file> [--ttl out.ttl]
python -m scripts.human_feedback interactive --mode … <target>           # approve / abort / edit / skip
```

The queue lists, most urgent first: escalations, low judge scores, mappings a judge
re-ran from a query, AIT edges a judge revised, and every dropped item (restorable).

| Operation | Meaning | Rule |
|---|---|---|
| `drop` | remove an item | full record kept; `restore` undoes it |
| `restore` | bring back a dropped item | only items an earlier round dropped; never a new item |
| `set` | change one field | field in `review_loop.FIXABLE` for that kind; value in its vocabulary, or copied from the evidence quote (`in_quote`) |
| `remap` | re-run the mapping tool with a search term | the tool decides the id; nothing found → refused. ABCD variables re-gate through the dictionary. An AIT edge may be re-pointed only by a **human**, who names the node (`mapped_by: human`) |
| `demote` | remove a mapping, keep the item | |
| `approve` | mark human-verified | NER/ABCD `human_verified`; AIT `human:curator` provenance |
| `note` | a remark | |

Refused operations are reported with the reason and change nothing. Every applied one
is logged in `review_loop.rounds` with actor, reviewer, value before and after; in NER
Turtle (`--profile full`) each human round is a `ner:HumanReviewActivity` with
`ner:ChangeRecord`s; in ABCD it is the "Review loop" table and `abcd:humanVerified`.

## 4. Output, and the optional output loop

After feedback the output is re-rendered and re-gated (NER TTL → `validate_ttl`;
AIT → `ait_tables validate`). The reviewer may go round again: interactive mode
returns to the queue after each round until they approve or skip.

## Per mode

**NER — framework:** `pipeline.py … [--feedback ops.json | --human-feedback interactive]`.
**NER — host model:** `batch.py init … --human-feedback` hands out a `human_feedback`
task after the judges (show the queue to the user, write their ops; `{"ops": []}`
approves). After delivery: `batch.py feedback <stem> --manifest …` writes the queue;
`… --ops file` applies it and re-renders the TTL (the output loop).

**ABCD:** `abcd_extract.py` runs concept map → judge → feedback per paper before
export and synthesis. With `--llm-model` the panel runs itself. On the agent path the
first run writes packets to `<out>/judge/<stem>/` and reports `judge: pending`; write
the reviews and run the same command again. `human_feedback queue --mode abcd
<out>/<stem>_abcd.json` writes `<out>/feedback/<stem>.feedback.json`, which the next
run applies automatically — so re-running the same command is the output loop.

**AIT:** after Pass 2a (`ait_evidence`) and Pass 3 (taxonomy mapping):

```
python -m scripts.review_loop map --mode ait out/          # concept map (trusted first)
python -m scripts.record_judge prepare --mode ait out/     # packets; then one judge at a time
python -m scripts.record_judge combine --mode ait out/     # corrections (superseding rows)
python -m scripts.human_feedback queue|apply --mode ait out/   # optional
python -m scripts.ait_gene_diff …                          # re-run if an edge moved to another node
python -m scripts.ait_tables validate out/                 # must exit 0
```
