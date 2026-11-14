# Search-to-review: from a research question to a screened corpus

The user has a question but no papers yet. The skill builds a reproducible
PubMed search, retrieves every record, screens titles and abstracts, and hands
the kept records to the Mode 3 review (full text → screening on full text →
charting → synthesis → exports). Discovery happens here, so the search
strategy is *generated and recorded*, not collected from the user.

Search is **PubMed only**. The full text of the kept records then comes from
several providers: open access (Unpaywall → OpenAlex → Semantic Scholar) and
the user's institutional EZproxy access, through `fetch_ezproxy.py`.

```
protocol intake (SKILL.md Mode 1)                       ← eligibility criteria
        │
        ├─ generate_search_strategy.py  question → MeSH-grounded PubMed query
        │     --fetch 0                 every record → results.jsonl (+ protocol.json audit)
        │     --provenance-out          → search_provenance.json (query, hash, date, count)
        │
        ├─ screen_records.py            title/abstract screening by the host agent
        │     plan → decisions per batch → status → apply
        │                               → corpus.json (include + unsure)
        │                               → screening_log.jsonl (every decision + reason)
        │                               → screening counts in search_provenance.json
        │
        ├─ fetch_ezproxy.py --corpus    full text: open access first, then institutional
        ▼
Mode 3 review (run_local_review.py, or agent-authored) → export → verify → manuscript → PDF
```

## 1. The search strategy

```bash
python scripts/generate_search_strategy.py "<question>" --emit-prompt > prompt.json
#   answer prompt.json k times (k = samples_needed, default 3) → samples.json
python scripts/generate_search_strategy.py "<question>" \
    --facet-samples samples.json --model-name <your model id> \
    --fetch 0 --outdir search/ --provenance-out search_provenance.json
```

Add the protocol's limits as filters when it has them: `--date-from 2015`,
`--date-to 2025/06/30`, `--languages english`, `--species humans`,
`--include-type "Randomized Controlled Trial"`, `--exclude-type Review`.

### Modes

| Mode | What the model does | Use |
| --- | --- | --- |
| `closure` (default) | marks **where** each PICO concept sits in the question; never sees MeSH. Sampled k times, majority-voted; vocabulary is then resolved from the local MeSH index alone | always, unless there is a reason not to |
| `llm` | proposes blocks and MeSH headings freely (one answer, `--llm-response`) | comparison only |
| `mesh_only` | nothing: every MeSH phrase in the question becomes a block | no model at all; a deterministic baseline |

Measured in search-strategy-generator's evaluation (6 questions × 3 runs, two
models): closure 0.97 / 0.97 / 1.00 / 0.84 on within-model, cross-model,
heading-Jaccard and PMID-Jaccard agreement; its former `hybrid` mode
0.90 / 0.76 / 0.95 / 0.68; `llm` 0.58 / 0.42 / 0.61 / 0.28.

### You are the model — no API key

Inside Claude Code, Codex or another agent host, **you answer the prompt
yourself**. Do not ask the user for `OPENROUTER_API_KEY`: it is only for running
the script on its own, outside an agent.

- `--emit-prompt` prints the exact messages the script would send to a model,
  `samples_needed`, and the answer format. Answer as that model would: the JSON
  object only.
- **Make the k samples independent**: a fresh context per sample (one subagent
  each, given only `prompt.json`'s messages). k answers written one after another
  in the same context are not independent, and the vote then measures nothing.
- Save the k answers as a JSON list (`[{...}, {...}, {...}]`) and pass it with
  `--facet-samples`. A malformed answer counts as a failed sample; the others
  still vote.
- Always pass `--model-name` with your model id. It is written into the output
  and the search provenance.

`--json` output records `provider` (`agent`, `openrouter`, or `none`), `model`,
the voted spans with their vote counts (`segmentation`), the concept blocks, the
compiled query and its hash. `provider: none` means no model was used (`mesh_only`,
or closure's offline heuristic when no answers and no key were given): report
that, do not describe it as model-generated.

### The MeSH index builds itself

The first run that needs it downloads NLM's compressed MeSH dump (~120 MB) and
builds the local index (~1–2 minutes, then reused; about 37 MB on disk). Tell
the user before it starts ("a one-time 120 MB MeSH download"), and run it with a
long timeout, or up front:

```bash
python scripts/generate_search_strategy.py --ensure-index
```

Do not ask the user to download or build anything. If the download fails
(exit 2), the message says how to build from a dump already on disk
(`--build-index mesh2026.nt[.gz]`). `SYNTHSCHOLAR_MESH_DB=/path/mesh.sqlite`
reuses an existing index.

### Check the strategy before fetching

Show the user the concept blocks and the hit count (`--count`) before fetching
thousands of records. A count far from expectations usually means a span
covered two concepts (e.g. "breast cancer patients undergoing chemotherapy"
resolves to `Breast Neoplasms` OR `Drug Therapy`, which is broader than meant).
Re-answer the prompt or add filters; never hand-edit the compiled query, since
that breaks its hash and its reproducibility.

## 2. Title/abstract screening

```bash
python scripts/screen_records.py plan search/<run>/results.jsonl \
    --protocol protocol.json --workdir screening/
#   for each screening/batch_NNN.json → write screening/decisions_NNN.json
python scripts/screen_records.py status --workdir screening/
python scripts/screen_records.py apply --workdir screening/ --corpus-out corpus.json \
    --provenance search_provenance.json --model-name <your model id>
```

`plan` needs the protocol's `inclusion_criteria` / `exclusion_criteria`, so the
Mode 1 intake comes first. `screening/instructions.json` holds the rules and
criteria; give it, with one batch, to whoever screens that batch.

**Decisions** (`{"decisions": [{"pmid", "decision", "reason"}]}`, every PMID
in the batch exactly once):

| Decision | When | Kept? |
| --- | --- | --- |
| `include` | plausibly meets every inclusion criterion, no exclusion applies | yes |
| `exclude` | clearly fails a criterion; **the reason names it** ("exclusion: review", "inclusion: post-treatment survivors") | no |
| `unsure` | the title/abstract does not say enough (no abstract, unclear population or treatment phase) | yes, assessed on full text |

When in doubt, `unsure`, not `exclude`: a wrong exclusion here is lost to the
review; a wrong inclusion is caught at full text. Judge only from the record's
own title and abstract.

**Batches are independent.** One agent can work through them in order; for a
large search, give each batch to its own subagent in parallel (about 25
records per batch; roughly 300 tokens per abstract). `status` lists any batch
that is missing or invalid; `apply` refuses until every batch is valid, and
refuses if `results.jsonl` changed after `plan`.

`apply` writes:

- `corpus.json`: the kept records in the corpus format `fetch_ezproxy.py` and
  `run_local_review.py` read (real PMIDs, `source: PubMed`, no full text yet),
  labelled `provenance: pubmed_search_screened`;
- `screening/screening_log.jsonl`: every decision with its reason, the
  screener (`--model-name`) and the time. Keep it with the review: it is the
  audit trail for the records excluded here, which never enter `review.json`;
- `search_provenance.json`: `records_screened`, `records_excluded_title_abstract`,
  `reports_sought`.

## 3. Full text, then the review

```bash
python scripts/fetch_ezproxy.py --corpus corpus.json --oa-only           # free first
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs    # + institutional
```

Then the Mode 3 review, with the generated provenance:

```bash
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json \
    --provenance search_provenance.json --outdir out/
```

or the agent-authored path (no key) — see `byo_corpus_review.md`. Records with
no retrievable full text are still assessed, on the abstract, and marked
`assessed_on="abstract_only"`.

## PRISMA flow: who did what

Unlike a bring-your-own corpus, every identification and screening number here
comes from the skill's own run, and the provenance says so:

| Flow field | Comes from |
| --- | --- |
| `total_identified`, per-database count | the PubMed search (`--provenance-out`) |
| `duplicates_removed` | 0 (one database; PMIDs are unique) |
| `screened_title_abstract`, `excluded_title_abstract`, `sought_fulltext` | `screen_records.py apply` |
| `not_retrieved`, `assessed_eligibility`, exclusions, `included_synthesis` | `fetch_ezproxy.py` and the review |

If `--fetch N` retrieved fewer records than PubMed reported, the provenance
note says so and `apply` warns: screen everything (`--fetch 0`) for a
systematic review.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `MeSH index not available` (exit 2) | download failed: check the network, or `--build-index` a local dump |
| `note: no model was called` | no `--facet-samples` and no key: answer `--emit-prompt` yourself |
| PubMed warns `quotedphrasesnotfound` | inverted MeSH entry terms ("Elder, Frail") match nothing as phrases; harmless for recall |
| `screen_records.py apply` refuses | run `status`: a batch is missing, a PMID is undecided or decided twice, or an exclusion has no reason |
| far more hits than expected | a span covered two concepts that got ORed; see *Check the strategy* above |
