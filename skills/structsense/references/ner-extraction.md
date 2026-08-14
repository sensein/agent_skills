# NER (named entity + key term) extraction

## Goal

Given unstructured text (paper, abstract, clinical note, news article), produce two lists:

- **`entities`** — typed mentions of real-world things (proteins, brain regions, species, drugs, methods, people, organizations, …).
- **`key_terms`** — domain-salient phrases that aren't full entities but matter for retrieval (e.g. "long-term potentiation", "single-cell RNA-seq", "supply-chain risk").

This is a span-extraction task — the model must preserve `start`/`end` offsets so downstream consumers can highlight text and verify.

## Pick the right extractor variant

This skill ships **three NER extractor prompts**, each with its own label taxonomy. Pick by the domain of the source text.

| Variant | Label taxonomy focus | Use when… |
|---|---|---|
| `prompts/extractor-ner-general.md` | Person, Organization, Location, Product, Event, Date, Time, Money, Percent, Quantity, Law, Language, Other | The text is news, finance, biographies, generic web content, customer support, or any non-specialized domain. Also a good first pass when you don't yet know the domain. |
| `prompts/extractor-ner-neuroscience.md` | Anatomy + cells + molecules + species + methods + measurements + behavior + disease across all of neuroscience | The text is a neuroscience paper, abstract, methods section, or review — *broad* coverage from behavior down to molecules. |
| `prompts/extractor-ner-cns-cells.md` | CellClass / CellType / CellSubtype + LineageMarker + Morphology + EphysProperty + Layer + Projection + AtlasReference + ProfilingMethod | The text is specifically about **CNS cells** — cell atlases, patch-seq, scRNA-seq cell typing, BICCN-style cell census, anything where the subject is "what cells live in this CNS region and how they differ." |

The three variants share the same output schema (`entities[]`, `key_terms[]` with `start`/`end`/`sentence`); only the `label` taxonomy differs. That means downstream alignment / judge / merge logic doesn't change when you swap variants.

When in doubt, run the **general** variant for a coarse first pass, then re-run with the more specific variant on chunks that contain domain-specific content.

## Running CNS cell extraction as the host model (no API key)

The CNS-cell variant is the one most often reached for, and the one where the
API-key confusion bites hardest — the `cns_cells` **NER profile** and an **LLM API
key** are unrelated things that both look like "setup". Neither is a prerequisite for
extracting. Concretely, in a Claude Code / Codex / Pi session:

**Step 1 — text in.** `input_loader.py` is the only script here with a plain CLI:

```bash
python -m scripts.input_loader paper.pdf --out paper.txt    # PDF/CSV/TXT/MD; no LLM
```

**Step 2 — pass 1 is you.** Read `prompts/extractor-ner-cns-cells.md`, follow it
against `paper.txt`, and write the JSON yourself. No `--extractor`, no key.

**Step 3 — mask-recall.** `mask_pass.py` is a **library, not a CLI** (its `__main__` is
a demo). Drive it from a short script:

```python
import json, pathlib
from scripts.mask_pass import mask_for_recall, map_masked_offsets_to_original

text = pathlib.Path("paper.txt").read_text()
pass1 = json.loads(pathlib.Path("paper.pass1.json").read_text())

masked, pmap = mask_for_recall(text, pass1["entities"])
pathlib.Path("paper.masked.txt").write_text(masked)
# …you now extract from paper.masked.txt using prompts/mask-recall-pass.md
# and write paper.pass2.json…

pass2 = json.loads(pathlib.Path("paper.pass2.json").read_text())
recovered = map_masked_offsets_to_original(masked, pass2["entities"], pmap, text)
pass1["entities"] += recovered
pathlib.Path("paper.merged.json").write_text(json.dumps(pass1))
```

Use `span_keys=("term", "start", "end")` for `key_terms`, whose surface field is
`term`, not `entity`.

**Step 4 — alignment is a TOOL call, not a model call** (rule 15): local hybrid at
`:8000`, else `BIOPORTAL_API_KEY`, else ask the user. Never invent CL IRIs.

**Step 5 — canonical shape + stats.** Pure Python, modifies in place:

```bash
python -m scripts.normalize_result paper.merged.json --input paper.txt \
       --out paper_final.json --llm-model claude-opus-5      # ← your own model id
```

**Step 6 — more than one paper? Merge them.** Per-paper files stay authoritative; the
corpus view is a separate deliverable, not a replacement (rule 9b):

```bash
python -m scripts.merge_corpus out/*_final.json \
       --out out/corpus_synthesis            # -> corpus_synthesis.json + .md
```

You get one canonical row per cell across the corpus, which papers it appears in, and
an explicit list of forms that different papers mapped to **different** CL ids. That
last table is the one worth reading before ingesting anything — exactness is
context-dependent, so a conflict is a curation decision rather than automatically a
bug, and the merge deliberately does not pick a winner.

**Do pass `--llm-model` here, with your own model identity.** It is a *provenance
label*, not a provider call — `normalize_result` never contacts anything; it writes
`source_model: "llm_ner:<value>"` on items that lack one. Omit it and every item you
extracted is tagged `llm_ner:unknown`, which violates rule 11 and makes the run
unauditable. The flag being named after a model is what makes it look like framework
plumbing; it isn't.

That is the sharpest instance of the general trap: a flag or variable whose name
mentions a model or a key is not automatically evidence that an API call is coming.
Check what the script does with it.

**What each optional piece actually costs you:**

| Piece | Needs | If you skip it |
|---|---|---|
| `prompts/extractor-ner-cns-cells.md` | nothing — you read it | you fall back to the broad neuroscience taxonomy and lose CellSubtype / LineageMarker / EphysProperty granularity |
| mask-recall pass | nothing — you re-extract | **–30–80% recall**, concentrated exactly in markers and ephys phrases. Do not skip this one on cell-typing text |
| `--ner-profile cns_cells` | `pip install transformers torch` (**no API key**) | you lose HF consensus (`source_model`, `consensus_count`); LLM-only extraction still works and is still exhaustive |
| concept mapping | local mapper at `:8000`, or `BIOPORTAL_API_KEY` | items stay `unmapped` — which is honest and allowed. Fabricated CL IRIs are not (rule 15) |
| judge | nothing — you score | no `judge_score`; the heuristic scorer still works offline |

**Scoring against a human gold standard?** Read
[cell-annotation-conventions.md](cell-annotation-conventions.md) first. Cell mentions
carry a specificity type (`cell_phenotype` / `cell_vague` / `cell_hetero`) that the
label taxonomy alone does not express, hedged spans nest around groundable ones, and a
conjunction takes one ontology id per element. Most apparent misses are these
conventions rather than missed cells — and one of them, the old rule 9 excluding
immune and vascular cells, was costing recall on exactly the injury and
neuroinflammation papers where cell diversity is the point.

**Cell-specific yield check.** Cell-typing text is dense, so the rule-8 sanity numbers
are higher here than for general NER. A single cell-atlas paper should land in the
**500–2000+** mention range with `mentions_per_unique > 1`. Two failure signatures
worth recognising in `stats`:

- `mentions_per_unique ≈ 1` on a cell paper → surface-form dedup crept in. "Pvalb"
  appears dozens of times in one paper; one row means something collapsed it.
- `CellType` heavily outnumbering `LineageMarker` + `EphysProperty` combined → pass-1
  captured the cell names and dropped their co-mentioned attributes. That is precisely
  what mask-recall recovers.

## Output schema

See `schemas/ner-output.schema.json` for the formal schema. Minimum required:

```jsonc
{
  "source_metadata": {           // ONCE per run — NOT per item
    "paper_title": "string",     // optional
    "doi":         "string",     // optional
    "source_path": "string"      // optional
  },
  "entities": [
    {
      "entity":   "string",      // surface form, exactly as in the text
      "label":    "string",      // entity type (see "Label taxonomy" below)
      "sentence": "string",      // the full containing sentence
      "start": 0,                // char offset of `entity` in original text
      "end":   0,                // char offset (exclusive) of `entity` end
      "paper_location": "string" // optional: section/page/paragraph (varies per mention)
    }
  ],
  "key_terms": [
    {
      "term": "string",
      "sentence": "string",
      "start": 0,
      "end": 0,
      "paper_location": "string" // optional
    }
  ]
}
```

> **Why `paper_title` and `doi` are NOT per-entity.** Earlier versions of this
> schema repeated these on every item. On a paper with 1500 entity mentions,
> that means 1500 copies of the same string — gigabytes wasted across a
> corpus run, and a needless burden on the LLM (which has to emit them
> faithfully on every item). Keep them once at the top level under
> `source_metadata`. `paper_location` (section / page / paragraph) DOES vary
> per mention and stays per-entity.

## Label taxonomy

Each extractor variant ships with its own closed taxonomy embedded in its system prompt. **Do not let the model invent new labels mid-run** — the parser should reject anything off-vocabulary and reprompt.

If the user wants a custom taxonomy:

1. Start from the closest variant.
2. Edit the `LABEL TAXONOMY` block in the prompt — keep it explicit, with one short description per label.
3. Update the alignment routing table (in the same prompt file) so each new label has a target ontology.
4. Test against a small annotated sample before running at scale.

## Span discipline

Two failure modes the model loves:

1. **Hallucinated spans.** Model emits an `entity` text that doesn't actually appear in the source. **Mitigation:** after parsing the JSON, verify `text[start:end] == entity` for every item. Drop or repair items that fail.
2. **Off-by-one offsets.** **Mitigation:** ask the model to also emit `sentence`, then verify the sentence appears in the text and that `entity` is a substring of `sentence` at the right relative offset.

```python
def validate_span(text, item):
    return (
        text[item["start"]:item["end"]] == item["entity"]
        and item["sentence"] in text
        and item["entity"] in item["sentence"]
    )
```

## Key-term vs entity disambiguation

A useful working rule:

- **Entity** if you could draw a line from the surface form to a row in a database (gene, drug, region…).
- **Key term** if it's a phrase a human would search for but isn't a single referent (workflow names, paradigms, technique families).

When in doubt, emit it as a `key_term`; the alignment stage handles both.

## Chunked extraction (long papers)

Long documents must be chunked. For NER, **chunk at sentence boundaries** so spans don't cross chunks. Pseudocode:

```python
chunks = split_into_sentence_chunks(text, max_chars=2000)
chunk_results = [extract_ner(chunk, base_offset=offset) for offset, chunk in chunks]
merged_entities = flatten([r["entities"] for r in chunk_results])
# Dedup by the full SPAN triple — NOT by surface form. Different occurrences
# of the same surface form have different (start, end) and must all survive.
merged_entities = deduplicate(merged_entities, key=("entity", "start", "end"))
```

`base_offset` matters: the extractor reports offsets relative to the chunk it sees. You must add the chunk's start offset back into the source text when merging — otherwise every chunk says "starts at 17" and the offsets are meaningless.

## Exhaustiveness — extract every occurrence

This skill is built around **exhaustive** NER. Models default to one-row-per-unique-surface-form behaviour unless you push them otherwise. All three extractor prompts now contain explicit "extract EVERY occurrence" language. **Every mention of every entity must be emitted as its own item**, with its own distinct `start`/`end`. A multi-page neuroscience paper should yield hundreds to thousands of entity items, not a few hundred.

If your yield feels far short of what `structsense` or a careful human would produce, two things to check first:

1. **Are you running the mask-recall pass?** See the next section.
2. **Are you post-deduplicating by surface form?** Don't. Span-level dedup (`(entity, start, end)`) is fine and preserves multiple occurrences; surface-level dedup destroys them.

## Two-pass strategy: mask-mode

The single most reliable way to push recall close to exhaustive is the **mask-recall pass**:

1. Run any of the three NER extractor prompts (pass-1).
2. Mask every extracted span in the source text with a `[E<i>]` placeholder, padded to preserve character offsets (`scripts/mask_pass.py:mask_for_recall`).
3. Re-run with `prompts/mask-recall-pass.md` over the masked text. The model surfaces mentions pass-1 missed.
4. Translate the new mentions' offsets back to the original text (`scripts/mask_pass.py:map_masked_offsets_to_original`).
5. Run span validation and append survivors to pass-1 results.

Typical recovery on a neuroscience paper is **+30–80%** more mentions over pass-1 alone — exactly the gap users notice between this skill and structsense.

For precision/quality work, also run **mask-verify** (`prompts/mask-verify-pass.md`): replace each extracted span (one at a time) with `[MASK]` and ask the model to predict the label from context. Disagreement signals candidate label errors that the judge can down-weight. See `scripts/mask_pass.py:mask_one_for_verify`.

Stop at two passes. Three or more gives diminishing returns and starts hallucinating mentions.

## Dedup rules

Span-level dedup is OK and recommended (it removes duplicates introduced when chunks overlap or pass-1 + mask-recall surface the exact same span twice). Surface-level dedup is **not** OK — it destroys multiple-occurrence information.

Across chunks (and within a chunk):

- Exact `(entity_text, start, end)` match → keep one copy.
- Same `entity_text` in different sentences → keep ALL copies (each is a distinct mention).
- Same `entity_text` in the same sentence at different positions → keep ALL copies.
- Same `entity_text` at the same `start`/`end` (true duplicate from chunk overlap or pass-1 + mask-recall surfacing the same span) → keep one copy.

## What to pass downstream

The alignment stage receives the full NER output verbatim. Don't reformat. Don't add a wrapper key. The next prompt expects:

```json
{"entities": [...], "key_terms": [...]}
```

If you wrap it as `{"extracted_data": {"entities": ...}}`, the alignment prompt's placeholder won't resolve and you'll get nonsense.

## Edge cases

- **Acronyms with expansions** ("hippocampus (HP)"): emit both the long form and the acronym as separate entities sharing a `label`. The alignment stage will collapse them to the same ontology IRI.
- **Negated mentions** ("no significant change in BDNF expression"): still emit the entity; let downstream consumers handle negation.
- **Tables and figure captions**: chunk them separately if possible — entity density is much higher there.
- **Equations / Greek letters**: usually not entities. The extractor should ignore them unless they name a measured quantity.
