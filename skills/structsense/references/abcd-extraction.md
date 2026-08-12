# ABCD / HBCD extraction and cross-paper synthesis

Pull out of publications **what an ABCD or HBCD study actually used and found** —
variables, constructs, models, findings — then compare across papers: where do
they agree, where do they contradict each other, and are particular variables
consistently treated as mediators or moderators?

The paper is the only source of *what was used*. The NBDC data dictionary and the
Cognitive Atlas are only used to **verify and join** what the paper says. Neither
is enumerated into the output.

## Two commands

```bash
# 0. once per release — build the dictionary you will verify against
pip install nbdctools
python -m scripts.abcd_dictionary build --study abcd --release latest
python -m scripts.abcd_dictionary build --study abcd --release 5.1   # add more for rename detection

# 1. single paper  ->  paper_abcd.json | .md | .ttl
python -m scripts.abcd_extract paper.pdf --llm-model openai/gpt-4o-mini

# 2. bulk + synthesis  ->  one set per paper, plus abcd_synthesis.{json,md,ttl}
python -m scripts.abcd_extract ./papers --bulk --synthesize --out-dir ./out
```

Every run emits all three formats: **JSON** (machine record, includes rejected
claims), **Markdown** (tables to read), **Turtle** (triples for a graph store,
PROV-O provenance). Restrict with `--formats json,md`.

## The three hard rules of this mode

### 1. Strict verification — a claim exists only if it is in the paper

Every item carries `evidence.quote`, copied verbatim. `scripts/abcd_verify.py`
searches the paper's text for that quote and **deletes items whose quote is not
there**, recording the reason in `rejected[]`. Whitespace, ligatures and
line-wrapping are normalised (a PDF text layer mangles these and no model
reproduces them exactly); the characters themselves must still be present, in
order. Quotes under 25 characters are refused — a fragment that short matches by
accident.

The requirement is **per-section**, because "present in the paper" differs by
claim type:

| Section | What must be in the quote | Why |
|---|---|---|
| `variables` | the variable name, literally | If the paper never writes `nihtbx_flanker_uncorrected`, we cannot say it used that variable. Matched on alphanumerics too, so PDF-mangled `ab g dyn visit type` still matches `ab_g_dyn__visit_type`. |
| `constructs` | nothing beyond the quote existing | A construct is a *reading* of the prose ("executive function was indexed by…") and legitimately never appears verbatim. `evidence.label_in_quote` records verbatim vs mapped. |
| `models`, `findings` | at least one referenced variable | A statement is a paraphrase, so requiring it verbatim would reject good items; requiring a variable it names keeps it tied to the text. |

Two independent gates then decide what an item may be *called*:

- **Variables** are only called ABCD/HBCD variables when
  `abcd_dictionary.resolve()` finds them in a real release snapshot →
  `dictionary_status: "verified"`. Otherwise `unverified_variable` (looks like a
  variable name but isn't in the dictionary) or `not_a_variable_name`. The item is
  kept and visible either way — never silently upgraded.
- **Constructs** only carry a Cognitive Atlas id that came back from a lookup. An
  id supplied by the model is discarded into `demoted_claim`, exactly as
  `iri_validation.py` demotes fabricated IRIs.

Check `verification` in the output before trusting a run:

```json
"verification": {
  "variables_dictionary_verified": 14, "variables_unverified": 2,
  "constructs_mapped": 5, "constructs_unmapped": 3,
  "rejected_total": 6,
  "by_section": { "findings": { "kept": 22, "rejected": 3,
                                "reason_no_referenced_variable_in_quote": 3 } }
}
```

`rejected_total: 0` on a long paper is a smell, not a triumph — it usually means
the extractor quoted loosely and the verifier was too lenient, or the model
emitted very little. A healthy run rejects a few claims.

### 2. Complete provenance — including where in the paper

Per item:

```json
"evidence": {
  "quote": "we used nihtbx_flanker_uncorrected as the primary cognitive outcome",
  "used_context": "…the surrounding sentences, i.e. the context it was used in…",
  "start": 12043, "end": 12110,
  "section": "Methods", "page": 4,
  "anchor_method": "re_anchored",
  "occurrences_in_paper": 3,
  "label_in_quote": true
}
```

`start`/`end` index the **original** text, so a reader can slice the source file
and see the quote. `anchor_method` says whether the model's offsets were right
(`as_reported`) or had to be re-found (`re_anchored`) — miscounting characters is
expected and forgiven; inventing the quote is not.

Per run, `provenance` records the extractor model, the text extractor
(GROBID/PyMuPDF/pdfminer) and char count, and — critically — **every dictionary
snapshot consulted**: study, release, variable count, retrieval method, source URL
and sha256, retrieval timestamp. Plus the Cognitive Atlas vocabularies with their
fetch time. A verdict is reproducible only if you know which dictionary produced
it.

In Turtle, all of this survives as triples (`abcd:quote`, `abcd:usedContext`,
`abcd:charStart`, `abcd:section`, `abcd:page`, `prov:wasDerivedFrom`,
`prov:hadPrimarySource`), so "where did this come from?" is a SPARQL query rather
than a re-read.

### 3. Single or bulk — same guarantees

`--bulk` walks a directory (`.pdf`, `.txt`, `.md`, recursive). One paper failing
does not abort the batch: the failure is reported, the rest continue, and the exit
code is `2` when some succeeded and `1` when none did. `--synthesize` adds the
cross-paper pass at the end. A bulk run of N papers produces N output sets plus
one synthesis, so per-paper evidence stays inspectable — the synthesis never
becomes the only record.

## Where the variable list comes from

`NBDCtools` keeps the ABCD/HBCD data dictionary in a companion R data package
(`NBDCtoolsData`, dataset `lst_dds`), keyed by study → release. `nbdctools` on
PyPI downloads and reads it **without R**.

```bash
python -m scripts.abcd_dictionary build --study abcd --release latest
python -m scripts.abcd_dictionary build --study abcd --release latest --all-releases
python -m scripts.abcd_dictionary info
python -m scripts.abcd_dictionary lookup nihtbx_flanker_uncorrected
```

If the download is blocked or `nbdctools` is unavailable, supply the dictionary
yourself — the mode does not degrade to guessing:

```bash
# in R
write.csv(NBDCtools::get_dd("abcd", "6.1"), "dd_abcd_6.1.csv")
# then
python -m scripts.abcd_dictionary build --study abcd --release 6.1 --from-csv dd_abcd_6.1.csv
```

Only `name` is required in a CSV export; `label`/`description` additionally enable
label matching for papers that name a measure in prose instead of by id.

**Load several releases.** Names change between releases, and papers cite
whichever release they analysed. With 5.1 and 6.1 both loaded, a variable present
in one and absent in the other yields `dd_release_gap` — usually a rename, and
exactly the thing a reader needs to know before comparing two papers. Without a
dictionary at all, `--allow-no-dictionary` proceeds with everything marked
`no_dictionary_loaded`; nothing is fabricated, but nothing is verified either.

Matching is deliberately narrow: exact name, then normalised name
(case/underscore differences), then a **full** label or description match.
No substring, no fuzzy scoring — a partial label match is not evidence that a
specific variable was used. `search` exists for exploring the dictionary by hand
and is never used to verify.

## Each variable carries the mention AND the mapping

The mention is the evidence; the dictionary entry is the interpretation. Both are
reported, because a reader needs to check the join:

| Field | Meaning |
|---|---|
| `mention_as_written` | Exactly how the paper wrote it — a variable id *or* a prose label ("NIH Toolbox Flanker Uncorrected Standard Score") |
| `dictionary_match.variable` | The dictionary variable it resolved to (`nihtbx_flanker_uncorrected`) |
| `dictionary_match.match_method` | `exact_name` · `normalized_name` · `label` · `description` — how the join was made |
| `nda_or_nbdc_table` | Dictionary table of the resolved variable (`nc_y_nihtb`) |
| `nbdc_domain` / `nbdc_sub_domain` | Dictionary domain (`Neurocognition` / `Executive function`) |
| `dd_releases_containing` | Releases (of those loaded) whose dictionary has this name |
| `dictionary_status` | `verified` · `unverified_variable` · `not_a_variable_name` · `no_dictionary_loaded` |

Column naming differs between the NBDCtools bundle and a portal/CSV export
(`table_name` vs `nda_or_nbdc_table`, `domain` vs `nbdc_domain`). The output always
uses the canonical names above, filled from whichever column the loaded snapshot
actually has, so a consumer sees one stable shape either way.

The Markdown table puts them side by side — *Mentioned as → Maps to variable →
nda_or_nbdc_table → nbdc_domain* — and the Turtle emits `abcd:mentionAsWritten`,
`abcd:dictionaryVariable`, `abcd:ndaOrNbdcTable`, `abcd:nbdcDomain`.

## Where the constructs come from

The Cognitive Atlas (~918 concepts, ~856 tasks) is the construct vocabulary. It
lets two papers using different measures — a Flanker score and a Stroop score —
join on one construct id, which is what makes "where is the consensus?" a
well-posed question.

```bash
python -m scripts.cognitive_atlas refresh                    # cache both vocabularies
python -m scripts.cognitive_atlas map "working memory" "inhibitory control"
python -m scripts.cognitive_atlas search inhibit             # propose candidates
```

Matching is exact name → singularised name → declared alias. Not fuzzy, on
purpose: "memory" must not silently become "working memory". Real consequence —
`working memory` and `impulsivity` map; `inhibitory control` and `internalizing
problems` do **not**, because the Atlas names those differently. Unmapped
constructs are reported honestly and still grouped in the synthesis by their
lowercased label (`unmapped:internalizing problems`). When something important is
unmapped, run `search` and offer the candidates to the user — do not auto-pick.

## Reading the synthesis

```bash
python -m scripts.abcd_synthesize ./out --min-papers 3 --out ./out/abcd_synthesis
```

**Counting is by paper, never by finding.** A paper reporting one association
across six models must not outvote five other papers; that would make verbosity
look like evidence.

Constructs:

| Verdict | Meaning |
|---|---|
| `consensus` | ≥ `min_papers`, one direction holds ≥ 70% of papers |
| `divergent` | papers report **opposing signs** (positive *and* negative) |
| `mixed` | no direction reaches the threshold, but no outright contradiction |
| `insufficient_papers` | fewer than `min_papers` — reported without a verdict |

Divergence means opposing signs, not differing magnitudes. Two papers reporting
`b = 0.02` and `b = 0.31` agree on direction; that is not a contradiction, and
calling it one would be the most common way to manufacture a finding.

Variable roles answer "is this consistently a mediator/moderator?":

| Verdict | Meaning |
|---|---|
| `consistent_role` | one role in ≥ 70% of papers — e.g. a stable mediator |
| `contested_role` | multiple roles claimed, none dominant — reported as contested, **not** resolved by majority |
| `mixed` | one role claimed but below threshold |
| `insufficient_papers` | too few papers to judge |

`unspecified` can never be a dominant role — it is the absence of a claim. This is
why the extractor prompt insists on `unspecified` when a paper is ambiguous about
mediator vs moderator: a guess there silently converts a contested role into a
false consensus.

Every row carries `evidence[]` with paper id, section and the verified quote, and
the Markdown puts contested roles side by side so the disagreement is legible
rather than asserted. `method` is emitted in the JSON so a reader knows the
thresholds without reading the code.

## Failure modes worth recognising

- **Scanned PDF, no text layer.** `abcd_extract` errors with "produced no text".
  OCR it first; do not let a model "read" an image and invent quotes.
- **Everything `unverified_variable`.** Usually the wrong release, or no
  dictionary. Check `provenance.dictionaries`, then `abcd_dictionary info`.
- **Everything `unmapped`.** The Atlas cache is missing (`refresh`) or the paper
  uses vocabulary the Atlas names differently. Use `search` and ask the user.
- **`rejected_total` very high.** The extractor is paraphrasing instead of
  quoting. This is the verifier working; re-run and, if it persists, the model is
  too small for verbatim quoting.
- **A construct with `paper_count: 1` and a confident verdict.** It cannot have
  one — check `min_papers`. A single paper is an observation, not a consensus.
