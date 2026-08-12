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
