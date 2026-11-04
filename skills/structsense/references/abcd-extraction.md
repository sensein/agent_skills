# ABCD / HBCD extraction and cross-paper synthesis

Pull out of publications **what an ABCD or HBCD study actually used and found** —
variables, constructs, models, findings — then compare across papers: where do
they agree, where do they contradict each other, and are particular variables
consistently treated as mediators or moderators?

The paper is the only source of *what was used*. The NBDC data dictionary and the
Cognitive Atlas are only used to **verify and join** what the paper says. Neither
is enumerated into the output.

## Install

```bash
pip install -r requirements.txt
```

**The dictionaries ship with the skill.** `data/dictionaries/` carries all seven
releases — ABCD `nda-legacy` (4.x/5.x), `6.0`, `6.1`, `7.0` and HBCD `1.0`, `1.1`,
`2.0` — 539,781 variables in 8.6 MB, minimal columns and gzipped (they are 250 MB+
raw). So extraction works out of the box with **no workbook, no network, no R**:

```bash
python -m scripts.abcd_dictionary info      # lists the bundled snapshots
```

A locally built snapshot always wins over a bundled one for the same
study+release, so rebuilding for a new release is a drop-in. `--from-xlsx` with no
path auto-discovers `NBDC_variable_catalog_full.xlsx` (skill `data/`, cwd,
`~/Downloads`, `~/Desktop`, `~`), so nobody has to hard-code someone else's
download path:

```bash
python -m scripts.abcd_dictionary build --study abcd --release 7.1 --minimal --gzip \
    --dir data/dictionaries          # workbook found automatically
```

That is the whole install for this mode with the agent as the model: `requests`,
`jsonschema`, `pymupdf` + `pdfminer.six` (PDF text), `openpyxl` (the catalog
workbook and .xlsx DOI lists). No GPU, no API key, no R.

Without a PDF extractor every paper fails with "all PDF extractors failed", which
is the most common first-run problem. `requirements-llm.txt` is only for the API
path (`--llm-model`); `requirements-ner.txt` is only for the unrelated NER
ensemble and pulls in torch, so skip it here.

## Two commands

```bash
# 0. build the dictionaries you will verify against (see below for sources)
python -m scripts.abcd_dictionary build --from-xlsx NBDC_variable_catalog_full.xlsx --all-sheets
python -m scripts.abcd_dictionary build --study abcd --release nda-legacy --from-nda

# 1. extract — ONE argument, auto-detected
python -m scripts.abcd_extract paper.pdf                --llm-model MODEL
python -m scripts.abcd_extract ./papers                 --llm-model MODEL
python -m scripts.abcd_extract paper_titles_dois.csv     --llm-model MODEL
python -m scripts.abcd_extract 10.1016/j.dcn.2021.100948 --llm-model MODEL
```

### Who runs the model: two paths

**In Claude Code, Codex or Claude Desktop, YOU are the model.** There is no API to
call, so `--llm-model` does not apply. Two steps:

```bash
python -m scripts.abcd_extract ./papers --prepare
# writes <stem>.txt per paper and prints a plan: text path, char count, and where to
# put the payload. Read each text, follow prompts/extractor-abcd.md yourself, write
# <stem>.payload.json, then:
python -m scripts.abcd_extract ./papers --payload ./papers
```

`--payload` accepts a single `.json` (one paper), a directory of
`<stem>.payload.json`, or a `.jsonl` keyed by `source_path`. Verification,
dictionary gating, construct mapping, synthesis and all three output formats are
byte-identical on this path — they are scripts, not prompts. Being the model is not
a licence to skip the quote rule: the verifier deletes whatever you cannot support,
and `provenance.extraction_path` records `agent_supplied_payload` with
`llm_model: "agent (no API call)"` so a reader knows who extracted.

**With Pi, a batch runner or a cron job, a framework calls an API.** Pass
`--llm-model` and the script does the extraction itself:

```bash
python -m scripts.abcd_extract ./papers --llm-model openai/gpt-4o-mini
```

Passing neither is an error rather than a guess — one choice spends API credits and
the other does not, so the script refuses to pick for you.

### The input is auto-detected — there is no bulk flag

| You pass | What happens |
|---|---|
| a `.pdf` / `.txt` / `.md` | that one paper |
| a **directory** | every paper under it, recursively |
| a **CSV / TSV / XLSX** with a DOI column | open-access PDF fetched per DOI |
| a **`.txt` of DOIs** | same (a `.txt` is read as a DOI list when it parses as one) |
| a bare **DOI** | that one paper, fetched |

More than one paper implies a cross-paper synthesis; one paper does not need one.
Override either way with `--synthesize` / `--no-synthesize`.

DOI columns are found by name (`doi`, `doi_url`, `article_doi`, …) and, failing
that, by scanning each row for a DOI pattern — but **never by title**, because
resolving a paper by fuzzy title match is how the wrong paper enters a corpus.

Fetching is **open access only**: Unpaywall (with `--email`, per their terms) →
OpenAlex → Semantic Scholar. Downloads are verified by magic bytes, so a publisher
paywall answering `200 text/html` is reported as `not_a_pdf` instead of being saved
as a "PDF" that later yields no text. Paywalled DOIs come back as unresolved with
the reason; download those yourself and point at the directory. Each fetch records
the service that answered, the URL, the license where known, and a sha256, into the
paper's `provenance.retrieval`.

Every run emits three formats by default: **JSON** (machine record per paper,
includes rejected claims), **Turtle** (triples for a graph store, PROV-O
provenance), and **one Excel workbook for the whole run**, `abcd_results.xlsx`,
which is the file to read. Its sheets are README, **Cross-paper** (one block per
relationship: a heading row with the verdict, then each paper's finding on it),
**Findings** (each paper's results in reading order, with claim type,
directionality and the cross-paper block it belongs to), Papers, Variables,
Models, Constructs, and Rejected when anything was rejected. Row 1 of each sheet
says what the sheet is for. Every row names its paper and carries the full quote,
plus the full sentence when the quote is a fragment. Per-paper
**Markdown** is opt-in (`--formats json,ttl,xlsx,md`). **codebook** is opt-in too
(`--formats ...,codebook`) and writes the run in the ABCD annotators' own coding
scheme for diffing against hand-coded data. To build the workbook from an
earlier run's output: `python -m scripts.abcd_xlsx <out_dir>`.

## Where the output goes

```
papers/                                  <- inputs, never written to
└── abcd_results/                        <- everything this skill produces
    ├── abcd_results.xlsx                one workbook: every paper + synthesis
    ├── <stem>_abcd.{json,ttl}           (.md with --formats ...,md)
    ├── <stem>_abcd.codebook.tsv         --formats ...,codebook
    ├── abcd_synthesis.{json,ttl}
    ├── text/<stem>.txt                  extracted text (--prepare)
    ├── payloads/<stem>.payload.json     agent payloads (--prepare -> --payload)
    ├── judge/<stem>/{packets,reviews}/  the judge panel's packets and reviews
    └── feedback/<stem>.feedback.json    a reviewer's ops (human_feedback queue)
```

### Concept map → judge → human feedback (every paper)

After verification each paper runs the review loop shared with NER and AIT
(`references/review-loop.md`), before export and before the synthesis counts it:

1. **Concept map.** Constructs (and findings' constructs) go through the mapping
   cascade with label route `CognitiveConstruct`: the trusted ontology files first
   (COGAT, COGPO, NBO, MF, MFOEM, MFOMD, HP in priority.md order), then the local
   mapper, OLS MCP, BioPortal. The Cognitive Atlas id from the verifier stays; the
   tool mapping is added as `ontology_id` / `ontology_mapping_source` and written
   as `skos:<tier>` in the Turtle. `--offline-mapping` = trusted files only.
2. **Judge.** The same panel and gates as NER (`scripts/record_judge.py`,
   `prompts/judge-record.md`): grounding (re-anchored quotes), labeling (role,
   respondent, construct kind, model kind), mapping (dictionary match, construct
   mapping), claims (direction, effect size, statistic from the quote; cited work
   fails). A gate failure moves the item to `rejected[]` as `judge:<judge>: …`.
   With `--llm-model` the panel runs (`--judge-model` to use another model). On the
   agent path the first run writes the packets to `judge/<stem>/` and reports
   `judge: pending`; write each review file, then re-run the same command.
3. **Human feedback (optional).** `python -m scripts.human_feedback queue --mode
   abcd abcd_results/<stem>_abcd.json` writes `feedback/<stem>.feedback.json`;
   fill its `ops` (or turn the user's words into ops with
   `prompts/humanfeedback.md`) and re-run the same `abcd_extract` command — the file
   is applied automatically. A variable `remap` re-gates through the dictionary; a
   value set on a finding must be copied from its quote. `--human-feedback
   interactive` asks per paper instead. Re-running is the output loop.

The Markdown's "Review loop" table lists every judge and human change.

`--out-dir` moves it. Two properties matter beyond tidiness:

- **The results directory is skipped when scanning inputs.** It contains a `.txt` of
  every paper, so a second run over the same corpus would otherwise process each
  study twice — once from the PDF and once from its own extracted text. Content
  checksums cannot catch that pair, since the two files genuinely differ.
- **`--payload` becomes optional.** If `abcd_results/payloads/` holds payloads, a bare
  `abcd_extract <input>` uses them. Reading files that already exist spends nothing;
  the refusal to guess exists to protect API credits, not to make you retype a path
  the script chose.

## The three hard rules of this mode

### 1. Strict verification — a claim exists only if it is in the paper

Every item carries `evidence.quote`, copied verbatim. `scripts/abcd_verify.py`
searches the paper's text for that quote and **deletes items whose quote is not
there**, recording the reason in `rejected[]`. Whitespace, ligatures and
line-wrapping are normalised (a PDF text layer mangles these and no model
reproduces them exactly); the characters themselves must still be present, in
order. Quotes under 25 characters are refused — a fragment that short matches by
accident.

**Quotes are whole sentences.** After an item passes verification and the
own-study gate, its quote is widened to the full sentence(s) it sits in, verbatim
from the same text, with `start`/`end` updated. The extractor's own span is kept
as `quote_as_extracted` and `quote_widened_to_sentence: true` is set. The quote is
widened after the gate, so this never changes what is kept. Table and figure
quotes stay as the row. A sentence that a figure, table or page break interrupts
in the extracted text ends where the break is and gets `sentence_cut_by_layout:
true`. The continuation is not guessed, because on a two-column page the next
block is often the other column. A finding whose `effect_size` holds two values
of one metric (`r = −.15 …; r = −.41 …`) is flagged `combines_several_results`;
the prompt asks for one finding per result.

**A summary must say no more than its evidence.** Every variable, construct and
statistic of the paper that a finding's `statement` names must appear in its
quote, or for a table row in the row plus its verbatim `table_context` (caption,
column headings, group heading, footnote, correlation-matrix row labels). A
finding that fails is **rejected** (`rejected[]`, reason
`summary_not_in_evidence`, with the unmatched names), not kept with a warning.
Verbs and timepoint wording are not checked.

**Text extraction keeps tables out of sentences.** The PyMuPDF backend reads each
page block by block, puts rotated text (a table printed sideways) after the
prose, and drops running headers and footers (blocks repeated on half the pages
or more). Its earlier sorted plain-text mode merged a sideways table into the
prose word by word ("Heightened familyvariables conflict … baselinewhen
controllingand …"), so quotes from that text could not be read or found in the
paper. Provenance now records which backend produced the text
(`provenance.text_extractor`) instead of `unknown`.

**One input per paper: duplicates are found by content, not by name.** Before any
extraction, every input is checked against the others:

1. **Identical bytes** (sha256): `paper (1).pdf`, a renamed copy, a copy in
   another folder.
2. **Same text**: a quick text read (PyMuPDF, else pdfminer; .txt/.md/.html read
   directly) is fingerprinted as word pairs. Two files are the same paper when
   ≥ 60% of the smaller one's word pairs appear in the other, or ≥ 40% plus the
   same DOI on the first page. Dataset DOIs (`10.15154/…`, NDA) don't count,
   because every ABCD paper cites them. On ABCD papers, copies of one paper
   score 0.85–1.00 (including a text dump whose layout wove a table into the
   prose) and different papers by the same authors on the same cohort score
   0.10–0.23. This catches a PDF plus its `.txt` dump, a results folder's
   `text/` copies, re-downloads with different bytes, and renamed copies.

Of a group of duplicates, the kept file is the original format (PDF first), then
the fuller text, then the oldest file, then the copy nearest the top of the
folder without a "(1)"/"copy" marker. Each skipped file is printed with the
reason and kept file, and listed in the run summary. A file whose text can't be
read (a scan with no text layer) is checked by checksum only.

Two *different* papers that share a file name are both processed, under distinct
output names. The original document keeps `<stem>_abcd.*`, and the other gets
`<stem>_<ext>_abcd.*` (or `<stem>_<folder>_<ext>`), so neither overwrites the
other and existing payloads still match the original.

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

**Best source: the NBDC variable catalog workbook** (`NBDC_variable_catalog_full.xlsx`,
downloadable from the NBDC Data Hub). One sheet per study+release — ABCD 6.0 / 6.1 /
7.0, HBCD 1.0 / 1.1 / 2.0 — with ~83–96k variables each and, crucially, the
alternate namings.

```bash
pip install openpyxl
# one release
python -m scripts.abcd_dictionary build --study abcd --release 6.1 \
    --from-xlsx ~/Downloads/NBDC_variable_catalog_full.xlsx
# every release sheet in the workbook, in one pass
python -m scripts.abcd_dictionary build --from-xlsx ~/Downloads/NBDC_variable_catalog_full.xlsx --all-sheets
```

Sheet names are parsed for study and release, so `--all-sheets` produces six
snapshots. Each is ~36 MB of JSON for an ABCD release and loads in under a second.

### Why the alternate namings matter more than anything else here

**ABCD 6.x renamed variables wholesale.** The Flanker administration variable is
`nc_y_flnkr_adm___1` in the 6.1 dictionary, but it is `neurocog_2_flanker___1` in
NDA and `neurocog_2_flanker` in DEAP. A 2022 paper cites the 5.x/NDA name
`nihtbx_flanker_uncorrected`, which appears **nowhere** in the 6.1 `name` column.

So the catalog's `name_nda`, `name_deap`, `name_redcap`, `name_short` and
`name_stata` columns are all indexed as ways in, and the match method records which
naming the paper used:

```
nc_y_flnkr_adm___1          -> nc_y_flnkr_adm___1              via exact_name
neurocog_2_flanker___1      -> nc_y_flnkr_adm___1              via nda_name
neurocog_2_flanker          -> nc_y_flnkr_adm___1              via deap_name
nihtbx_flanker_uncorrected  -> nc_y_nihtb__flnkr__uncor_score  via nda_name
bogus_variable_xyz          -> UNVERIFIED
```

Without this, virtually every pre-6.0 paper would report `unverified_variable` for
variables that are perfectly real. `nda_or_nbdc_table` comes from the catalog's
`table_nda` (`abcd_tbss01`) and `nbdc_table` keeps the NBDC table (`nc_y_nihtb`), so
both namings survive into the output.

### Releases 4.x and 5.x: the NDA data dictionary

The workbook (and NBDCtools, and the NBDC portal) covers **6.0 and later** — NBDC
releases start there; 4.x and 5.x were the NDA/DEAP era. For those, NDA's data
dictionary is public and authoritative:

```bash
python -m scripts.abcd_dictionary build --study abcd --release nda-legacy --from-nda
```

One request lists the structures, one per structure lists its elements — 292
`abcd_*` structures, ~86k elements. Responses are cached per structure, so a
rebuild is free and an interrupted run resumes without re-fetching (a first run
that lost 4 structures to a DNS blip completed on retry in 24s instead of 10
minutes). Requests are throttled; this is a public API being asked for a few
hundred documents.

Load both eras together and a paper from either resolves, with the bridge visible:

```
nihtbx_flanker_uncorrected  ->  nda-legacy  nihtbx_flanker_uncorrected        exact_name
                            ->  6.1         nc_y_nihtb__flnkr__uncor_score    nda_name
cbcl_scr_syn_internal_r     ->  nda-legacy  cbcl_scr_syn_internal_r           exact_name
                            ->  6.1         mh_p_cbcl__synd__int_sum          nda_name
smri_vol_cdk_banksstslh     ->  nda-legacy  smri_vol_cdk_banksstslh           exact_name
                            ->  6.1         mr_y_smri__vol__dsk__bstmps__lh_sum  nda_name
abcd_made_up_var            ->  UNVERIFIED
```

`releases_for()` then reports `['nda-legacy', '6.1']`, which is what a reader needs
before comparing a 2021 paper with a 2025 one.

### Alternative: NBDCtools

`NBDCtools` keeps the same dictionary in a companion R data package
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
python -m scripts.abcd_dictionary build --study abcd --release 6.1 --from-csv dd_abcd_6.1.csv

# or a DEAP variable export (abcd.deapscience.com -> my-datasets -> create dataset),
# or an NDA data-dictionary download — CSV or TSV, headers as they come
python -m scripts.abcd_dictionary build --study abcd --release 6.1 --from-csv deap_export.tsv
```

**Headers are matched flexibly**, because these sources spell them differently.
`name` accepts `element_name` / `variable_name` / `variable` / `element` /
`item_name` / `field_name` / `short_name`; the table column accepts
`nda_or_nbdc_table` / `table_name` / `structure` / `instrument`; the domain accepts
`nbdc_domain` / `domain` / `category`; labels accept `element_description` /
`variable_label` / `title`. Delimiter is sniffed (CSV/TSV) and a UTF-8 BOM is
tolerated. The translation is recorded in the snapshot's provenance
(`csv_header_mapping`, `csv_headers_ignored`) so a reader can see which of the
export's columns became `name` and which were dropped. An export with no
recognisable name column fails loudly, listing the headers it did see.

Only the name column is required; label/description additionally enable label
matching for papers that name a measure in prose instead of by id.

**DEAP itself cannot be read programmatically here.** `abcd.deapscience.com` is a
single-page app whose API sits behind NDA-approved login, so the integration point
is your export, not a scrape. Same for the NDA portal.

### Release ids are cross-checked against the public documentation

```bash
python -m scripts.abcd_dictionary releases
# 6.0    https://docs.abcdstudy.org/latest/documentation/release_notes/6_0.html
# 6.1    https://docs.abcdstudy.org/latest/documentation/release_notes/6_1.html
# 7.0    https://docs.abcdstudy.org/latest/documentation/release_notes/7_0.html
```

`docs.abcdstudy.org` is prose documentation, not a machine-readable dictionary, but
it is a public list of which releases exist. Building a snapshot records the
citation URL (`documentation_release_notes`) and whether the release is documented
(`release_documented`) in provenance, and warns when it is not. **Advisory only** —
the docs site can lag or restructure, so it never blocks a snapshot that is real.
Useful when a paper states a release like "4.0" that the current documentation no
longer lists.

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

## A role belongs to an analysis, not to a variable

This is the single most common way an ABCD extraction reads as wrong to somebody
who knows the paper. A study runs brain metrics as the **outcomes** of preterm
birth, then re-uses them as **mediators** of gestational age on cognition. A study
regresses Y3 on X1 while controlling for Y1 and Y2, so one measure is the outcome
at one wave and a covariate at two others. `variables[].role` holds one string and
has to pick, and whichever it picks makes the other analysis vanish.

So the pipeline derives roles from `models[]`, which already record what each
analysis did, and reports them per analysis:

| Field | Meaning |
|---|---|
| `role` | The single headline value. Still there, still one string. |
| `roles[]` | Every role the variable plays across the models, in precedence order |
| `role_assignments[]` | `{model_id, role, as_written, section}` — which analysis, in its own words |
| `role_varies_by_analysis` | True when `roles` has more than one |
| `role_summary` | `"outcome (M1, M8); mediator (M4, M5)"` — what the Markdown prints |
| `role_basis` | Where the role came from (below) |
| `bidirectional_in[]` | Models where it is both predictor and outcome — a cross-lagged design, not a contradiction |

`role_basis` is one of:

- `paper_statement_confirmed_by_model` — the prose and the model arrays agree.
- `model_declaration` — they disagreed and the arrays won. `role_conflict` keeps
  both readings; nothing is overwritten silently.
- `paper_statement` — no analysis names the variable, so the extractor's reading
  stands alone.
- `prior_wave_control` — the one inference the pipeline makes, described below.
- `unresolved` — nothing says what it is. Reported as `unspecified`, not guessed.

The Markdown gets a **Roles by analysis** table: variables down the side,
analyses across the top, the role in each cell. That is the view that answers
"what did this paper actually do, and when" without reading the JSON. The Turtle
emits an `abcd:RoleAssignment` node per assignment, linking the variable to the
`abcd:StatisticalModel` that gave it the role.

### Analyses that assign no roles

A correlation matrix puts every variable on both axes. Read literally, its
"predictors" and "outcomes" arrays claim a direction the paper never asserted —
in one run this made family income a predictor of itself. Models marked
`kind: descriptive` or `kind: correlational` (or, absent a `kind`, recognised from
their specification: bivariate correlations, descriptive statistics, measurement
invariance, attrition checks) are listed but contribute no roles.

### The one inference: `prior_wave_control`

An autoregressive design measures Y at three waves, models the last, and adjusts
for the earlier ones. The earliest wave often surfaces only in Table 1, so no model
names it and it lands as `unspecified` — reading as a variable the study never
used, when in fact it is a control.

The pipeline promotes it to `covariate` only where the paper has already shown its
hand: **some other wave of the same measure is a declared covariate**, and an
outcome wave sits later than the entry in question. Without a declared covariate
wave, nothing is inferred — the alternative is turning every descriptives row into
a control the paper never mentioned. Where it fires, `role_inference` records the
rule, the covariate waves and the outcome waves that licensed it.

## One measure, several waves

The extractor sees the same instrument written three ways — "internalizing
behaviors" in the Measures section, "Internalizing Time 2" in a coefficient table,
"Internalizing problems year 1" in Table 1 — and emits three entries. They are one
measure at three waves, and left as three names a reader cannot tell, while the
synthesis reports three variables where the papers used one.

Two fields fix the identity:

- `timepoint_normalized` / `timepoint_order` — the wave parsed into a comparable
  integer (0 = baseline). A `baseline` cue beats everything, then an explicit
  follow-up year, then `Time N` / `wave N` (which count from one, so `Time 2` is
  wave 1), then a bare `year N` (which follows ABCD's own event naming and counts
  from baseline). Cues that cannot be reconciled leave `timepoint_order` null —
  a wrong order rewrites which measurement was the outcome. A wording spanning
  waves ("baseline through 3-year follow-up") gets `timepoint_span` instead.
- `measure` / `measure_key` — the instrument shared by every wave of it. Entries
  are grouped when one entry's *name*, stripped of wave tokens, appears among
  another's stripped wordings. Keying on a name rather than on any shared alias is
  what stops a generic alias ("income", "sex") chaining two different measures
  together, and two entries that resolved to *different* dictionary variables are
  never grouped however alike they read — `fes_y_ss_fc` and `fes_p_ss_fc` are
  different respondents.

Derived quantities stay separate. A growth-curve intercept or slope, a change
score, a residual or a composite is not the measure it was estimated from, so a
wording that adds one of those words is its own measure even though it repeats the
parent name.

Duplicate entries are merged on `(measure, wave)` rather than on the wording, so
"family income" at `"baseline"` and "Income Time 1" at `"baseline (Time 1)"` stop
standing as two variables the study apparently measured separately.

## Comparing a run against human coding

`--formats codebook` writes `<stem>_abcd.codebook.tsv` in the columns an ABCD
annotator fills in — *Text Content · Source · Codes* — plus *Role · Timepoint ·
Analyses · Section*, so a run can be diffed against a hand-coded gold standard
rather than eyeballed. The codes are the annotators', which are coarser than the
schema: `predictor` → **Independent Variable**, `outcome` → **Dependent
Variable**, `covariate`/`confounder`/`control` → **Covariate**, everything else →
**Additional Variables**. A variable's own quote is emitted beside it as **IV
Supporting Info** / **DV Supporting Info**, and the document-level fields map to
**ABCD Release**, **ABCD Time Point**, **Sample Size**, **Participant** and
**Participant Age**. The `Role` column keeps the finer distinction the human
scheme has no code for, so nothing is lost in the translation.

### Sentence-level annotation (the Sentences sheet)

A human annotator works sentence by sentence: `paper_id · sentence_id · section ·
text · is_claim · claim_type · causal_language · direction · hedged · notes`, with
cross-references in the notes ("restates 105", "paper says DMN here and DAN
there"). The verifier produces the same table as `sentences[]` in the JSON and the
**Sentences** sheet of the workbook (`scripts/abcd_sentences.py`):

- **Numbering.** Every prose sentence from the abstract to the conclusion is
  numbered in the order of the extracted text, with its section ("Results - 3.4
  Prenatal Tobacco Exposure ..."). Table blocks, running headers, licence lines
  and reference entries are skipped. Two-column PDFs put headings out of reading
  order, so where a finding quotes a sentence the extractor's section wins, and
  sentences between two that agree inherit it. A "References" heading that the
  layout put before the Conclusion does not end the numbering. Ids are this
  tool's; another annotator's numbering can differ by the front matter they count.
- **is_claim.** `yes` when a finding or a sentence claim quotes the sentence;
  `possibly (no finding extracted)` for a Results sentence that reads as a result
  but has neither (orange in the sheet); `not assessed` for abstract, discussion
  and conclusion sentences when the payload carries no `sentence_claims`; `no`
  otherwise.
- **sentence_claims.** The extractor's claims outside the findings: the abstract,
  the discussion's restatements, the conclusion. Each is anchored to a sentence id,
  rejected if it reports cited work, and carries `restates` and `conflicts_with`
  (resolved to ids). An inconsistency is shown in both sentences' notes and the
  rows are yellow.
- **causal_language** (`correlational` / `direct_causal` / `conditional_causal`)
  and **hedged** are set on prose findings too, from the extractor or, when it
  gave none, from the wording ("did not impact" is a negated causal verb, so
  correlational).

Findings stay result-level, which is finer than a sentence: a table row is a
finding with no sentence id, and its **Section** reads "Results - 3.3 Societal
Factors ... › Table 3", the section of the first sentence that cites the table.

### Sections are read from the paper, not from a fixed list

Papers name their sections differently ("Findings", "Study 2", "Analytic
Strategy", "Sample", "General Discussion", APA papers with no Introduction
heading at all). The verifier reads the paper's own outline
(`abcd_sentences.walk`) and gives each heading a role:

1. its words, when they say it (generic vocabulary: method, participants,
   measures, analysis, results, findings, study N ... / introduction, background,
   present study ... / discussion, limitations, conclusion ...);
2. otherwise its parent heading ("3.2 Prenatal Exposure ..." under Results);
3. otherwise its position: after an own-study heading it is own-study, after the
   Discussion it is discussion, before both it is introduction.

Headings are numbered ("2.5.1. Behavioral similarity analysis"), named, ALL
CAPS, letter-spaced ("A B S T R A C T") or short title-like lines in the paper's
own words; lines that repeat through the paper are running headers; numbered
lines that do not continue the paper's numbering (correlation-matrix rows) and
affiliations are not headings; after the abstract's Keywords line an APA paper's
introduction starts. Back matter (Funding, Author contributions, References ...)
is skipped, but running prose the column layout put there (the Discussion's last
paragraphs after "Funding") is kept.

The scope gate uses that role (`evidence.section_role`), so an extractor never
has to relabel a section "Results" to pass. The extractor's label is a hint: when
the outline says introduction/discussion but the label says results and the
quote has no citation, the quote is kept and the disagreement recorded
(`scope_signals.section_disagreement`), because two-column layouts put headings
out of order. Table rows are recognised from the outline too ("inside Table 3").

### A variable keeps one name

The verifier, not a rename, connects a variable to its quote. A quote mentions
`prenatal tobacco exposure` if it writes the name, an alias that occurs in the
paper, an abbreviation the paper defines ("prenatal tobacco exposure (PTE)", or
an "Abbreviations:" list), or — for a network pair "Default mode–Dorsal
attention" — both halves in any of those forms ("between the DMN and DAN").
`evidence.surface_in_quote` / `referenced_as` record how the quote wrote it. A
quote that refers back ("These models can also mutually predict each other") is
checked against the sentence before; `variables_named_in_previous_sentence`
records it.

### Direction conventions

- **Significance.** A finding whose own statistics (a table row: its
  `statistic`) are all non-significant against the paper's stated threshold
  ("an FDR of < 0.05 was considered significant"), else .05, is `null`; the
  sign moves to `direction_as_extracted`. `significance` holds the p values.
- **Change over time.** Direction is the direction of the change: positive
  increases or appears, negative decreases or weakens, null stays the same,
  mixed both. "Higher than .14 of 4th year" is a decrease (the later wave is
  lower). Verbs of change ("we found a decreased similarity") win over
  comparatives that compare tasks within a wave. Same rule for Findings and
  Sentences.
- **Signs in statistics.** "r = .14", "ranging from .03 to .08" state a sign.
- **Merging.** A sentence with one sign plus non-significant results reads as
  that sign; mixed means opposite signs.

### Claim types and the human scheme

`prediction` (model-based prediction: CPM, machine learning, predicted vs
actual — not regression wording) and `change_over_time` are claim types. Every
finding and sentence also gets `claim_type_human` in a sentence-level
annotator's scheme: association, group_difference (comparison), prediction,
change_over_time, descriptive. The workbook README has the crosswalk.

### Workbook columns say where they come from

Every header carries its source in brackets: (verbatim), (paper heading),
(agent), (agent summary, not verbatim), (agent, copied), (agent + tool), (agent
or tool), (tool). The README sheet defines each, and every Sentences column
(is_claim, causal_language, direction, hedged, notes).

On Yin 2025 (10.1016/j.biopsycho.2025.109058), from the existing payload, against
the sentence-level human annotation: 107 of 128 human sentences match a tool
sentence (the rest are figure/table captions and author contributions; before
the outline fixes 92, with the Discussion's last paragraphs lost), is_claim
agrees on 90 of 107 (11 more are flagged "possibly (no finding extracted)"), and
direction on 23 of 36 shared claims (9 before). The remaining differences are
mostly the annotator assigning a sign the sentence does not state ("significantly
associated", sign not reported; used positive).

### Checks the sentence layer adds

On Vishnubhotla 2026 (10.1002/brb3.71168) against the human annotation:

| Problem | What the verifier does now |
|---|---|
| A quote joining two sentences ("food insecurity was linked with poorer outcomes. Being Black/African American ...") | Cut to the sentence the finding comes from; variables only the other sentence names are dropped and listed. A sentence that leans on the previous one ("Additionally, SCT was ...") keeps it as context, so its subgroup survives. |
| Prose naming a network its cited table does not have ("within the default mode network"; Table 5's within-network row is Dorsal attention–Dorsal attention) | `cited_table_check: network_not_in_cited_table`, with the table's rows. Fires whether or not the extractor emitted the within-network result. |
| "All 9 scales" when two Table 3 rows are not significant | `cited_table_check: cited_table_partly_supports`, read from the clause about the finding's own predictor (PTE's "all scales", not PME's "7 out of 9"). |
| A sign the sentence does not state ("PTE was significantly associated with all scales") | Direction becomes `unspecified`; the extractor's sign is kept in `direction_as_extracted`. The table rows keep their own signs. |
| A table printing a pair in both orders (Default mode–Dorsal attention and Dorsal attention–Default mode) | The second row gets `mirror_of` and is counted once in Cross-paper. |
| A Results sentence with no finding | `possibly (no finding extracted)` in Sentences, listed in `verification.sentence_level`. |

With the updated prompt the run agrees with the human annotation on is_claim for
108 of the 109 sentences both have (the other 13 human rows are table captions and
notes, which are not sentences here), and on direction for 46 of the 49 shared
claims; two of the three differences are human labels the comparison itself
questioned (105, 107).

## Each variable carries the mention AND the mapping

The mention is the evidence; the dictionary entry is the interpretation. Both are
reported, because a reader needs to check the join:

| Field | Meaning |
|---|---|
| `mention_as_written` | Exactly how the paper wrote it — a variable id *or* a prose label ("NIH Toolbox Flanker Uncorrected Standard Score") |
| `dictionary_match.variable` | The dictionary variable it resolved to (`nihtbx_flanker_uncorrected`) |
| `dictionary_match.match_method` | `exact_name` · `normalized_name` · `nda_name` · `deap_name` · `label` · `label_context` · `label_context_family` · `label_context_domain` · `instrument_label` · `nda_element_api` — how the join was made |
| `nda_or_nbdc_table` | Dictionary table of the resolved variable (`nc_y_nihtb`) |
| `nbdc_domain` / `nbdc_sub_domain` | Dictionary domain (`Neurocognition` / `Executive function`) |
| `dd_releases_containing` | Releases (of those loaded) whose dictionary has this name |
| `dictionary_status` | `verified` · `verified_via_nda_api` · `context_variable` · `context_family` · `context_domain` · `instrument_table` · `ambiguous` · `unverified_variable` · `not_a_variable_name` · `no_dictionary_loaded` |
| `context_mapping` | The full audit when the mapping came from wording: cues that fired, ranked candidates with scores, thresholds, and why one variable was or was not named |

Column naming differs between the NBDCtools bundle and a portal/CSV export
(`table_name` vs `nda_or_nbdc_table`, `domain` vs `nbdc_domain`). The output always
uses the canonical names above, filled from whichever column the loaded snapshot
actually has, so a consumer sees one stable shape either way.

The Markdown table puts them side by side — *Mentioned as → Maps to variable →
nda_or_nbdc_table → nbdc_domain* — and the Turtle emits `abcd:mentionAsWritten`,
`abcd:dictionaryVariable`, `abcd:ndaOrNbdcTable`, `abcd:nbdcDomain`.

## Mapping the paper's wording, not just its variable names

`Dictionary.resolve()` answers "is this string a variable name?". Most papers never
satisfy it — they write prose. On a three-paper sample, name-only resolution placed
1 of 57 variables; `nda_or_nbdc_table` and `nbdc_domain` were empty for the rest,
which is the entire point of the mapping.

`scripts/abcd_context.py` matches the paper's phrasing against dictionary **labels**,
which are rich enough to make this lexical rather than speculative:

```
fes_y_ss_fc              Conflict Subscale from the Family Environment Scale
                         Sum of Youth Report (RAW Score)
fes_p_ss_fc              Conflict subscale from the Family Environment Scale
                         Sum of Parent Report (RAW Score)
nihtbx_list_uncorrected  NIH Toolbox List Sorting Working Memory Test Age 7+
                         v2.0 Uncorrected Standard Score
nihtbx_list_v            NIH Toolbox List Sorting Working Memory Test Age 7+
                         Version
```

Four signals decide between those, and every one is the paper's own words:

| Signal | Field | Effect |
|---|---|---|
| Instrument | `variables[].instrument` | Scopes candidates to that instrument's table. `externalizing` appears in the CBCL, the ABCL, the YSR and the Brief Problem Monitor — the paper naming the CBCL settles it, and no amount of scoring can. |
| Respondent | `variables[].respondent` | Filters, not penalises: "children completed the FES" excludes every parent-report variable. `fes_y_ss_fc` and `fes_p_ss_fc` are different measures. |
| Metric | `variables[].metric` | "fully corrected T-scores" picks `nihtbx_cryst_fc`; any metric cue sinks the administrative siblings (Version, Language, ItmCnt, DateFinished) that share every content word with the measure. |
| Release | `source_metadata.data_release` | Decides which snapshot is eligible. A 5.0 paper matched against 6.1 turns one clear measure into rival candidates in two tables — the single most valuable filter here. |

Two lexical bridges are built in, because without them some measures share no
content word with their label at all: light inflection folding ("externalizing
behaviors" ↔ the CBCL's "External … Scale") and a short table of documented naming
differences (axial ↔ longitudinal diffusivity, radial ↔ transverse, functional
connectivity ↔ network correlation, surface ↔ cortical area). A substitution is
recorded in `context_mapping.context_cues.synonym_applied`, so it can be rejected.

**It will not name a variable the paper did not name.**

| Status | When | What you get |
|---|---|---|
| `context_variable` | one candidate wins by the margin | variable + table + domain + release |
| `context_family` | several variables in one table fit equally well — 68 Desikan-Killiany ROIs for "cortical thickness" | table + domain + `family_prefix` (`smri_thick_cdk_*`) + candidates; variable `null` |
| `context_domain` | tables disagree, the domain does not (FA lives in several per-atlas tables) | domain + candidate tables |
| `instrument_table` | the paper named an instrument, not a variable | table + domain for that instrument |
| `ambiguous` | candidates spread across tables | nothing claimed, candidates listed |

### The NDA element API

```bash
python -m scripts.abcd_nda_api element nihtbx_flanker_uncorrected
python -m scripts.abcd_nda_api search "conflict subscale family environment scale"
```

`element()` confirms a name the paper printed that no bundled release contains —
status `verified_via_nda_api`, with the element's structures and aliases. Useful; it
is the one thing a snapshot cannot do.

Full-text element search is available and its results are **suggestions, not
mappings** (`nda_api_suggestions`). Two reasons. Every structure NDA can return is
already in the loaded snapshots, so a search hit is something the context matcher
saw and rejected. And NDA ranks lexically across the whole archive: asked about
"internalizing behaviors" it returned an *Adult* Behavior Checklist score, and "age
at time of scan" returned an SST series timestamp. Hits are intersected with this
study's tables, admin elements are dropped, and what survives is offered to a human.

`--nda-api auto` (default) confirms printed names always but only searches for runs
of at most 25 papers — a 770-paper corpus would mean thousands of requests.
Responses cache under `~/.cache/structsense/nda_api`, so a rerun is offline.

## Claim type and directionality of a finding

Each `findings[]` item has a `claim_type` and a `directionality`, alongside the
`direction` that the synthesis counts.

- `claim_type`: `explicit` · `implicit` · `comparison` · `correlation` ·
  `observation` · `associative` · `descriptive` · `causal`. The first five are
  the Claim Framework of C. Blake, "Beyond genes, proteins, and abstracts:
  Identifying scientific claims from full-text biomedical articles", *Journal of
  Biomedical Informatics* 43(2):173–189, 2010
  ([doi:10.1016/j.jbi.2009.11.001](https://doi.org/10.1016/j.jbi.2009.11.001)).
  The other three extend it. Definitions, examples and the precedence for
  sentences that fit several types are in `prompts/extractor-abcd.md`
  ("Claim types").
- `directionality`: `increase` · `decrease` · `mixed` · `support`. This is what
  the claim asserts. `support` means the relationship holds but no sign is
  given. A reported null effect has `directionality: null` and
  `direction: "null"`.

The verifier maps common variants to these values (`comparisons` →
`comparison`, `increased` → `increase`) and sets anything else to `null`
instead of guessing. Both fields appear in the Markdown findings table and in
the Turtle (`abcd:claimType`, `abcd:directionality`).

## Only what this study did

A paper's introduction and discussion are largely about other people's work, and
none of it belongs in the output. `gate_scope` decides, per item, whether the
evidence is the paper speaking about itself:

| Item | Bar |
|---|---|
| variable / construct | fails only if the evidence is purely somebody else's: a literature section, a citation or prior-work phrasing, and no first-person framing |
| finding | must be in a results-bearing section (Method/Results/Table/Abstract) **or** framed in the first person, and must not be attributed to cited work |

Rejections carry a reason (`finding_attributed_to_cited_work`,
`measure_only_mentioned_in_cited_work`) and the signals behind it in
`scope_signals`. This is not tidiness: without the gate, paper A's summary of paper
B arrives in the synthesis as independent evidence, and a literature that repeats
one original study looks like replication.

## Did the extraction get everything?

`coverage` answers it directly:

```json
"coverage": {
  "variables_declared": 38, "variables_referenced": 32,
  "declared_coverage": 1.0,
  "referenced_but_not_declared": []
}
```

Anything named in a `models[]` array or a `findings[].variables[]` array but never
declared in `variables[]` is listed. Those entries reach the synthesis with no
quote, no table and no domain while looking like ordinary variables, so the list is
a to-do for the extraction rather than a statistic. The prompt names the five places
variables hide: the descriptive-statistics table, the covariate list, the Measures
section, per-wave instances, and self-computed composites.

## Where the constructs come from

The Cognitive Atlas API (https://www.cognitiveatlas.org/api) is the construct
vocabulary: ~918 concepts, ~857 tasks and ~221 disorders (ADHD, conduct disorder,
obsessive-compulsive disorder — what CBCL scales are named after). It lets two
papers using different measures join on one construct id, which is what makes
"where is the consensus?" a well-posed question.

```bash
python -m scripts.cognitive_atlas refresh                    # download all three vocabularies
python -m scripts.cognitive_atlas map "working memory" "conduct disorder"
python -m scripts.cognitive_atlas api-search "inhibitory control"   # raw /search results
python -m scripts.cognitive_atlas id trm_4a3fd79d096be       # look an id up
python -m scripts.cognitive_atlas search inhibit             # substring search of the cache
```

Endpoints: `/api/v-alpha/concept`, `/task`, `/disorder` (whole vocabularies),
`/search?q=` (live search) and `/<kind>?id=` (one entry). A term is mapped by
exact name → singular → alias in the downloaded vocabularies, then, if that
misses, through the live search endpoint, accepting only a concept, task or
disorder whose name or alias equals the term. Every hit records the API URL that
produced it (`mapping_source`), and the run's provenance lists the vocabularies'
retrieval times and the number of live searches. The vocabulary cache is
re-downloaded after `STRUCTSENSE_COGATLAS_MAX_AGE_DAYS` (default 30); if the API
is down the stale cache is used and marked so. `--offline-atlas` uses the cache
only and makes no search calls.

Not fuzzy, on purpose: "memory" must not silently become "working memory". Search
results that are contrasts (`cnt_`, one task condition minus another) are never
accepted, and an alias counts only on an entry that lists at most two, because
"internalizing" lists "worry, anxiety, negativity, depression" as related terms,
not synonyms — "depression" does not become "internalizing". Hyphens fold to
spaces, so "obsessive compulsive disorder" maps to the disorder the API names
"obsessive-compulsive disorder". A model-supplied id is looked up through the API
and still demoted unless the term maps to it; the demotion records whether the id
exists and what the API calls it. Unmapped constructs are reported honestly and
grouped in the synthesis by their lowercased label (`unmapped:internalizing
problems`). When something important is unmapped, run `api-search` and offer the
candidates to the user — do not auto-pick. Independently of the Atlas, every
construct is also looked up in the trusted ontology files (the review loop's
concept-map step), so an Atlas miss can still carry a tool-verified COGAT / NBO /
MF / HP mapping.

## Reading the synthesis

```bash
python -m scripts.abcd_synthesize ./out --min-papers 3 --out ./out/abcd_synthesis
```

**Counting is by paper, never by finding.** A paper reporting one association
across six models must not outvote five other papers; that would make verbosity
look like evidence.

### Evidence quality of each finding

Levels follow the GRADE Working Group (Atkins D, et al. Grading quality of
evidence and strength of recommendations. *BMJ* 2004;328:1490):

| Level | Interpretation |
|---|---|
| High | Further research is highly unlikely to have a significant impact on our confidence in the claim |
| Moderate | Further research is moderately unlikely to have a significant impact on our confidence in the claim |
| Low | Further research is moderately likely to have a significant impact on our confidence in the claim |
| Very low | Further research is highly likely to have a significant impact on our confidence in the claim |

As in GRADE, observational evidence starts **low** and randomised evidence
**high**. A finding then moves one level for each of these: +1 longitudinal and
covariate-adjusted (the finding is about an outcome of a model with covariates,
and is not a bivariate correlation); +1 large standardized effect (|r| or |β| ≥
.30, d ≥ .80, ratio ≥ 2; unstandardized b is not judged); −1 no effect size and
no statistic; −1 n < 1,000; −1 subgroup only. The steps are in
`strength.reasons`. This adapts GRADE to single findings from what the paper
reports. It is not a GRADE review: risk of bias, inconsistency and publication
bias are not assessed.

### Claim status of each relationship

Each relationship's claim (its majority sign, or "there is an association" when
only nulls exist) gets a status on the scale at
<https://www.ncbi.nlm.nih.gov/books/NBK543866/>: established · provisionally
established · arguably true · speculative · arguably false · provisionally ruled
out · ruled out. Evidence *for* is findings with the claim's sign. Evidence
*against* is the opposite sign or a reported null. Each paper counts once per
side, at its best quality.

| Status | Rule here |
|---|---|
| established | high-quality evidence, nothing against, replicated in ≥ 2 independent cohorts |
| provisionally established | moderate-quality evidence (or unreplicated high), nothing against |
| arguably true | low-quality evidence with nothing against, or moderate+ evidence with more papers for than against |
| speculative | anything else: evidence on both sides, a disputed sign, or only very low quality |
| arguably false · provisionally ruled out · ruled out | the same rules applied to the negation |

All-ABCD corpora cannot reach *established*: the papers share one cohort, so
agreement is not an independent replication, and the status says so. Change
over time is not graded (`claim_status: null`).

### Relationships: where papers actually disagree

`relationships[]` groups findings by **what they are about**: the resolved
variables, not the construct alone. Two results disagree only when they concern
the same relationship and have opposing signs. Grouping by construct alone made
"income → lower family conflict" (negative) and "financial strain → higher family
conflict" (positive) look like papers disagreeing about family conflict.

**Notation.** `A ↔ B` means *A is associated with B*. The two-headed arrow
is deliberate: a relationship is keyed on the unordered pair of variables, so
one block pools correlations (no direction), A predicting B, and B predicting A.
It never asserts which variable predicts or causes the other; the direction is
in each finding. `A / B / C` is a finding about three or more variables (usually
a mediation path), listed alphabetically, not in causal order. `A over time` is
change or stability of one variable. A `→` inside a summary or quote is the
paper's own direction and is never added by the grouping.

| `kind` | When |
|---|---|
| `association` | two variables (growth factors kept apart: an intercept is not a slope) |
| `multi_variable` | three or more (mediation paths) |
| `change_over_time` | one variable, or several that each change over time ("both decreased over time"). Listed, never judged across papers: a stability coefficient and a mean decline share sign words, not meaning, so the verdict is `change_over_time` |
| `unspecified` | no variables, or a moderator with nothing else named |

Verdicts are the construct ones (`consensus`, `divergent`, `mixed`,
`insufficient_papers`, `no_directional_finding`), plus `change_over_time`.
`within_paper_conflict` lists papers that report both signs for one relationship,
which is usually an abstract contradicting its own results. Each relationship's
`evidence[]` has every paper's finding with effect, statistic, n, strength,
section, page and quote, plus `finding_index` back into that paper's `findings[]`.

A construct claim whose opposing signs all come from different relationships now
has the verdict `different_relationships` (not `divergent`), and its statement
names which relationships go which way. Trend findings are kept as `not_counted`
rather than counted as an association's direction.

### Claims

`claims[]` is the reading view: one claim per construct, then the evidence paper by
paper, then — separately — the contradictions and the caveats.

```
C2: Papers disagree on the direction of the association involving externalizing
    behaviours: both positive and negative effects are reported across 2 papers.

Evidence
  10.1007/s10826-…  release 5.0  positive  b = 0.184, p < 0.001, n = 11,868
                    strength: strong (n = 11,868; longitudinal, three annual waves)
Contradictions
  10.1017/S0954579…  release 5.0  negative  b = -0.330  strength: strong
Caveats
  - papers report the same sample size — likely the same children, so agreement is
    not independent
```

The strength rating is derived only from what the papers reported — sample band
(≥5000 large / 1000–4999 moderate / <1000 small), whether the design is
longitudinal, whether an effect size was printed, whether the result is
subgroup-only — and `strength.reasons` lists every input, so the label can be
argued with rather than trusted. Contradictions are kept out of the evidence list
on purpose: a claim supported by two papers and contradicted by one is not the same
thing as a claim supported by three.

### Constructs

| Verdict | Meaning |
|---|---|
| `consensus` | ≥ `min_papers`, one direction holds ≥ 70% of the direction claims |
| `divergent` | papers report **opposing signs** (positive *and* negative) |
| `mixed` | no direction reaches the threshold, but no outright contradiction |
| `insufficient_papers` | fewer than `min_papers` — reported without a verdict |
| `no_directional_finding` | the construct was studied but no signed effect was reported for it |

`agreement` is the majority direction's share of the **paper-direction claims**, not
of papers. With papers as the denominator, a construct where two papers said
positive and one of them also said negative printed `1.00` agreement beside a
`divergent` verdict — two numbers contradicting each other on one row.

Each construct also carries `measured_by`: the variables the papers *declared* as
operationalising it, with table, domain, release and paper ids. Variables that merely
appear in its findings are listed separately as `variables_in_findings` — conflating
the two produced "internalizing behaviours measured by financial strain", which no
paper said.

Divergence means opposing signs, not differing magnitudes. Two papers reporting
`b = 0.02` and `b = 0.31` agree on direction; that is not a contradiction, and
calling it one would be the most common way to manufacture a finding.

Variable roles answer "is this consistently a mediator/moderator?":

| Verdict | Meaning |
|---|---|
| `consistent_role` | the dominant role recurs in ≥ 70% of papers **and** is the only substantive role in ≥ 70% of them |
| `contested_role` | multiple roles claimed — reported as contested, **not** resolved by majority |
| `mixed` | one role claimed but below threshold |
| `insufficient_papers` | too few papers to judge |

Exclusivity is the second half of that first rule for a reason: a variable that is
a mediator in every paper *and* an outcome in every paper scored 1.00 on share alone
and was reported as a consistent mediator. `role_exclusivity` is the share of papers
where the dominant role stands alone.

Rows merge across papers on the resolved dictionary variable, then on a
paper-declared alias, then on the normalised mention (case and plural folded —
"family income" and "Family income" used to be two rows with one paper each). Never
on similarity: parent-report and youth-report versions of a scale stay separate,
and when two wordings resolve differently the row carries `mapping_disagreement`
instead of a silent pick.

Every variable row carries `paper_evidence`: per paper, the wording used, the
instrument, respondent and metric the paper stated, the roles and timepoints, what
it resolved to, its table and domain, the **dictionary release that mapping holds
in**, and the quotes. Every paper row carries the dataset it analysed (release,
sample, analytic sample, design, waves, cohort, sites, source) — which is how
"three papers agree" can be read as "three papers agree, all analysing the same
11,868 children".

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
