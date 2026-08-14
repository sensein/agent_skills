# Cell-mention annotation conventions (human gold standard)

Conventions taken from a human-annotated BioC corpus of cell mentions in
neuroscience/neuroimmunology papers. **The corpus itself is deliberately not
reproduced here** — it is held out as a validation set, and pasting its spans or
identifiers into this skill would leak the answers into every run that reads it. What
follows is the annotation *contract*, with neutral placeholder examples.

Read this together with `prompts/extractor-ner-cns-cells.md`. Where the two disagree,
this file wins — it describes what a human annotator actually did.

## 1. Cell mentions carry a specificity type, not just a label

This is the largest gap between the skill's own taxonomy and the gold standard. The
skill labels cells by *what kind* (`CellClass` / `CellType` / `CellSubtype`); the gold
standard also labels them by *how groundable* they are. The two axes are orthogonal —
emit both.

| Type | What it marks | Gets a CL id? |
|---|---|---|
| `cell_phenotype` | a cell whose identity is stated well enough to ground: a named type, a class, a marker-defined population that corresponds to a real cell | yes |
| `cell_vague` | identity deferred or hedged — the text names a *set* of cells without saying which: "… subtypes", "… types", "subsets of …", "subpopulations of …", or a bare marker-plus-cell construction (`<MARKER>+ cells`) that names no type | **no** — identifier is null |
| `cell_hetero` | a deliberately heterogeneous population or mixture: "immune cells", "support cells", a named mononuclear-cell fraction, a negatively-defined set (`non-<MARKER>+ cells`) | sometimes, and then loosely (see §4) |

The distinction that matters most in practice: **a marker does not make a cell type.**
`<MARKER>+ cells` with no type named is `cell_vague` even though it looks specific;
`non-<MARKER>+ cells` is `cell_hetero` because the complement of one marker is a
mixture. Both are extremely common in lineage-tracing and reporter-line papers.

A `cell_vague` or `cell_hetero` item is **not** a failed extraction. Emitting it with a
null identifier is the correct, complete answer, and it is what rule 15's
"`unmapped` is honest" clause is for. Do not invent a CL term to make a vague mention
look resolved.

## 2. Nested spans are expected — annotate the hedge AND the head

When a hedged wrapper contains a groundable term, both are emitted, usually sharing a
start offset and differing only in length:

```
"<TYPE> subtypes"          -> cell_vague,      no id          (the whole span)
"<TYPE>"                   -> cell_phenotype,  CL:…           (the inner span)
```

Same for `"subsets of A, B and C"` (vague, whole span) plus each of `A`, `B`, `C`
individually. So a single stretch of text can legitimately yield an outer vague
annotation and two or three inner phenotype annotations.

Consequences for the pipeline:

- **`start`/`end` overlap is legal here.** The skill's rule 4 (no duplicate
  `(entity, start, end)` triple) still holds — these differ in `end` — but any
  downstream de-overlap step must not collapse them.
- `mask_pass.mask_for_recall` resolves overlaps by **keeping the longest span**, so
  masking a nested pair hides the inner phenotype mention. Mask the outer vague spans
  and the inner phenotype spans in **separate passes** if you want recall on both.

## 3. Coordinated mentions: one annotation, one identifier slot per element

Conjunctions are annotated as a single span whose identifier is a **positional,
`;`-separated list** — one slot per coordinated element, in text order:

```
"A and B cells"            -> CL:<A> ; CL:<B>
"A- or B-<SUFFIX>"         -> CL:<A> ; CL:<B>
```

Slots with no CL equivalent are written `-`, holding the position:

```
"A and B"                  -> - ; CL:<B>          (A has no CL term)
"A, B, and C neurons"      -> - ; - ; CL:<C>
```

So the slot count must equal the number of coordinated elements even when most are
ungroundable. Getting this wrong silently shifts every mapping by one position.

## 4. Mapping precision is qualified, and `,` means something different from `;`

Every identifier carries a SKOS match qualifier:

- `(skos:exact)CL:…` — the mention *is* that class.
- `(skos:related)CL:…` — near but not equal: a qualified or derived population, a
  tissue-level term standing in for its cells, a marker-narrowed subset of a broader
  class.

**Two separators, two meanings — do not mix them:**

| Separator | Meaning |
|---|---|
| `;` | coordinated **mentions** — slot *i* maps element *i* of the span (§3) |
| `,` | multiple candidate **IRIs for one mention** — the annotator could not choose a single class |

So `(skos:related)CL:aaa,CL:bbb` is *one* cell mapped to two candidates, while
`(skos:exact)CL:aaa;(skos:exact)CL:bbb` is *two* cells in one span.

**Exactness is context-dependent, not a property of the string.** The same cell name
can be `skos:exact` in one paper and `skos:related` in another — a stem-cell-derived
or reporter-line population is *related* to the canonical class, not identical to it.
Two rules that generalise:

- Adding a marker qualifier downgrades the match: bare `<TYPE>` may be exact, while
  `<MARKER>+ <TYPE>` is related.
- Negation is always at best related, and usually `cell_hetero`.

## 5. Scope — wider than the current prompt says

**In scope, and easy to miss:**

- **Acronyms and abbreviations are cell mentions.** Papers introduce a cell type once
  and then use the acronym dozens of times. Every occurrence is annotated, including
  singular/plural variants of the same acronym. This is where exhaustiveness pays.
- **Figure captions.** The gold standard annotates `fig_caption` passages, and they
  are among the densest — a methods-heavy caption can name one cell type six times.
  Do not skip captions when chunking a paper.
- **Cell names embedded in mechanism or acronym names** still count. If a named
  mechanism contains cell names, the cell substrings are annotated individually even
  though the mechanism itself is not a cell.
- **Non-CNS cells in CNS papers.** Immune, haematopoietic, epithelial and vascular
  cells are annotated when they appear — injury, neuroinflammation and glial-scar
  papers are full of them.

> **This last point contradicts `prompts/extractor-ner-cns-cells.md` rule 9**, which
> declares "cells from PNS, immune system outside microglia, or non-neural tissue are
> OUT OF SCOPE — do NOT emit them." Against this gold standard that rule is a false-
> negative generator on exactly the papers where cell diversity is the point. Rule 9
> has been narrowed accordingly; if you are working from an older copy of the prompt,
> ignore it and extract the immune and vascular cells.

**Out of scope:**

- **Dataset-local cluster identifiers** — `cluster <N> cells`, `<CellPrefix> <N>`, and
  similar run-specific numbering are *not* annotated as cell mentions. They name a row
  in someone's analysis, not a cell type, and they do not transfer between papers.
- Named taxonomic clusters from a published taxonomy remain in scope as `CellSubtype`
  (the skill's existing guidance). The line is **published taxonomy vs. this paper's
  numbering** — not "does it contain a digit".

## 6. Offsets: BioC is (offset, length), this skill is (start, end)

The gold standard is BioC XML, where a location is `offset` + `length`. This skill's
schema uses `start` + `end` (exclusive). Converting:

```python
start = offset
end   = offset + length
```

Passage-level `<offset>` is `0` in this corpus, so annotation offsets are already
passage-relative — but check it per passage rather than assuming, because BioC allows a
non-zero passage offset and then every annotation is document-relative.

Whichever direction you convert, re-verify `text[start:end] == surface_form` before
trusting it (the skill's rule 2). `scripts/span_validator.py` does this.

## 7. Recall failure modes, measured

From an actual evaluation against this gold standard. Precision came out high (~0.94:
almost everything predicted inside an annotated passage was a real gold mention) while
recall sat around 0.36 — and most of that gap was **not** missed cells. Two structural
mismatches and four vocabulary classes account for it, in descending order of size.

**Scope was the largest single cause, and it was self-inflicted.** 142 of 736 gold
annotations were cells the old rule 9 told the extractor to skip: pituitary endocrine
cells, immune cells other than microglia, and vascular / epithelial / mesenchymal
cells. Recall on that slice was 0.12 — as instructed. Dropping the exclusion moved
overall recall from 0.359 to 0.416 on its own. This is why §5 says to emit them.

**Nesting was the second.** 21.6% of gold annotations overlap another gold annotation
(§2). The gold is multi-granularity; an extractor that emits one span per region
structurally cannot match both layers. Giving a prediction credit for the spans it
genuinely contains moved recall to 0.470 — meaning **~11% of the apparent miss rate
was a scoring artefact, not a model failure.**

Then the vocabulary classes, which are real gaps worth fixing:

| Class | What it looks like | Why it is missed |
|---|---|---|
| **Adjectival / attributive forms** | `neuronal`, `glial`, `astrocytic` used where the noun never appears | the largest single vocabulary gap by count. Lexicons hold nouns; a model asked for "cell types" tends to skip the adjective even though the gold annotates it |
| **Per-paper abbreviations** | a type introduced once as `Full Name (ABBR)` and then used only as `ABBR` for the rest of the paper | the abbreviation is not in any general vocabulary — it exists only in that document |
| **Coordinated / compound mentions** | `A and B <TYPE>s` | weakest recall of the three specificity types (~0.12 for `cell_hetero`). A matcher splits the conjunction into its parts and never emits the whole span, which is the one the gold marks |
| **Bare heads inside a longer match** | the plain noun inside a span that was already matched at a coarser granularity | same root cause as nesting: one span per region means the inner head is unavailable |

Three rules follow, and they are in the prompt as 9f–9h:

- **Emit adjectival and attributive forms.** If the text says `neuronal` where it means
  neurons, that is a cell mention. Do not require the canonical noun.
- **Build a per-paper abbreviation map before extracting.** Scan for
  `Full Name (ABBR)` / `ABBR (Full Name)` definitions, then treat every later
  occurrence of `ABBR` as a mention of that type. An acronym used 20 times after one
  definition is 20 mentions.
- **For a conjunction, emit the whole span AND each element.** Not one or the other —
  §2 nesting and §3 slot mapping both depend on the whole span existing.

**One practical trap that is not an extraction problem at all.** 19 passages could not
be located in the PDF text layer: figure captions and table cells, whose text a PDF
extractor reorders or drops. §5 says captions are in scope and dense — but that is
unachievable if the text you extracted never contained them. Before blaming recall,
confirm the captions are actually present:

```bash
python -m scripts.input_loader paper.pdf --out paper.txt
grep -ciE '^\s*(figure|fig\.?|table)\s*[0-9]' paper.txt   # 0 on a caption-heavy paper = the text layer dropped them
```

If they are missing, re-extract with GROBID (`--grobid-url`, which parses structure
rather than reading the text layer) rather than tuning the extractor. Passages you
cannot locate should be **excluded from the denominator**, not counted as misses —
otherwise a PDF-parsing limitation shows up as a model deficiency forever.

## 8. Validation checklist

When scoring an extraction against this gold standard, check these before concluding
the extractor is bad — most apparent errors are convention mismatches:

| Check | Failure signature |
|---|---|
| Did you emit the specificity type at all? | every gold `cell_vague` / `cell_hetero` item counts as a miss |
| Did you keep nested spans? | recall drops on the inner phenotype term, or on the outer hedge, depending on which your de-overlap step kept |
| Do coordinated slot counts match? | one span's mapping looks right and every later slot is off by one |
| Did you read `;` as `,`? | conjunctions collapse into one over-mapped cell |
| Did you exclude immune/vascular cells? | recall collapses specifically on injury and neuroinflammation papers |
| Did you include local cluster ids? | precision drops on scRNA-seq results sections |
| Are offsets converted from (offset, length)? | every span short by its own length, or shifted |
| Did you emit adjectival forms? | the largest single vocabulary gap; `neuronal` alone was the top miss |
| Did you expand per-paper abbreviations? | a type defined once and used 20 times costs 20 mentions |
| Did you emit whole coordinated spans? | `cell_hetero` recall collapses to ~0.12 |
| Are unlocatable passages excluded from the denominator? | a PDF text-layer limitation is scored as a model failure |
| Is nested credit given? | ~11 points of apparent miss rate that is a scoring artefact |

Report **in-scope and out-of-scope recall separately**, and say which exclusions are in
force. A single recall number over a gold standard whose scope differs from the
extractor's is not interpretable — the first evaluation's 0.359 was really 0.470 once
scope and nesting were accounted for, and neither adjustment involved changing the
extractor.

Also expect **precision to be understated**. Spurious predictions in that run were few
and mostly legitimate cell mentions the annotators simply had not marked in that
passage. Before tuning anything down, read a sample of the false positives: a
gold standard is a record of what one annotator marked, not an exhaustive census.

Score `cell_phenotype` mapping accuracy **separately** from span recall. They fail for
different reasons — spans fail on the conventions above, mappings fail on the rule-15
cascade — and a single blended number tells you which almost never.

## 9. Formal schemas, and the corpus view

- `schemas/cell-ner-output.schema.json` — the per-paper shape. It enforces what this
  file describes where a schema can: the `specificity` enum, `coordinated_elements ≥ 1`,
  the closed cns-cells label taxonomy for LLM-extracted items, and the rule that a
  `cell_vague` item must carry a **null** `ontology_id` (a vague mention names no type,
  so any id there is a fabrication). `ner-output.schema.json` stays task-agnostic.
- `schemas/cell-ner-corpus.schema.json` — the roll-up from
  `python -m scripts.merge_corpus <out>/*_final.json --out <out>/corpus_synthesis`, which every multi-paper run
  should produce alongside the per-paper files (SKILL.md rule 9b).

Two things the corpus view adds that per-paper scoring cannot see:

- **`ontology_conflicts`** — one canonical form mapped to different ids in different
  papers. Given §4, that is often legitimate rather than an error, which is exactly why
  the merge surfaces it instead of resolving it.
- **`specificity` null on a corpus row** — the same surface form was a grounded
  phenotype in one paper and part of a hedged set in another. Worth inspecting: either
  the papers genuinely differ, or one extraction applied §1 inconsistently.
