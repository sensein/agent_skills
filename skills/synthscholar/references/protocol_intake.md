# Guided protocol intake

This is the **field catalog** for a SynthScholar review (`ReviewProtocol`) — the
question, why it matters, an example, validation, default, and tier for every
input. It is **not** a script to read aloud field by field.

**Use it via the draft-and-confirm pattern (see SKILL.md → Mode 1):** draft the
whole protocol from whatever the user gave you (often just a title), using the
defaults below; interactively lock only the 2–4 **pivotal** decisions as
structured question cards; fill everything else silently; then show the full
draft and get one confirm. Validate with `scripts/validate_protocol.py` before
running. Capture asked decisions as provenance — see
[Provenance](#provenance-of-the-intake).

Tiers: **[R] required** (must end up non-empty — draft if the user didn't say),
**[+] recommended** (draft a default), **[A] advanced / domain** (default
silently; only raise if the user asks). A **★** marks the fields that are
*usually* the pivotal decisions worth an explicit question — judge per topic.

---

## 1. Scope & question

| Field | Tier | Question | Example | Validation |
| --- | --- | --- | --- | --- |
| `title` | [+] | "Working title of the review?" | "ML models for EEG seizure detection" | non-empty |
| `objective` | [R] | "Objective / research question?" | "Do CNNs outperform classical ML for seizure detection on scalp EEG?" | non-empty; defaults to title if blank |

## 2. PICO framework

| Field | Tier | Question | Example |
| --- | --- | --- | --- |
| `pico_population` | [R] | "Population?" | "Adults with epilepsy undergoing scalp EEG" |
| `pico_intervention` | [R] | "Intervention / exposure?" | "Deep-learning seizure detection models" |
| `pico_comparison` | [+] | "Comparator?" | "Classical ML (SVM, random forest) or clinician review" |
| `pico_outcome` | [R] | "Outcome(s)?" | "Sensitivity, specificity, false-alarm rate" |

At least Population + Intervention + Outcome should be non-empty; Comparison may
legitimately be N/A for single-arm scoping reviews.

## 3. Eligibility criteria

| Field | Tier | Question | Example |
| --- | --- | --- | --- |
| `inclusion_criteria` ★ | [R] | "Inclusion criteria (what makes a study eligible)?" | "Peer-reviewed; human scalp EEG; reports sensitivity/specificity; 2015+" |
| `exclusion_criteria` | [R] | "Exclusion criteria?" | "Animal studies; intracranial-only; no quantitative performance metric; non-English" |

Guide the user to make these **decidable at screening** — each criterion should
map to a yes/no test a screener (human or LLM) can apply.

## 3b. Research questions — what the review answers

| Field | Tier | Question | Example |
| --- | --- | --- | --- |
| `research_questions` ★ | [R] | "What questions should the review answer about every included study?" | see below |

```json
"research_questions": [
  {"question_id": "RQ1.1", "question": "Which participant groups are compared, and is each group's size reported?",
   "theme": "Participants", "short_title": "Groups and sizes"},
  {"question_id": "RQ4.5", "question": "How are robustness and generalisability evaluated?",
   "theme": "Computational techniques", "short_title": "Validation strategy"}
]
```

Each question is asked of **every** included study during charting (answered
into that article's `custom_fields`, keyed by `question_id`) and reported
question-first in all three exports: an appendix in the Markdown, the
`research_questions` block in the JSON, and `slr:ResearchQuestion` nodes in the
RDF. Rules that matter:

- **Number them.** `question_id` groups the report by its major number (`RQ1`),
  keys each article's answers, and becomes the question's IRI. Reuse the user's
  own numbering when they have one; never renumber it.
- **`theme` groups the report** — questions sharing a theme are reported
  together, in protocol order. Defaults to the major id when empty.
- **`short_title`** is the section label; derived from the question text when
  empty, which is usually worse than writing one.
- **Make each answerable from one paper.** "How has the field evolved?" is a
  synthesis question, not a charting question — it produces 170 shrugs.
- Ids must be unique; `validate_protocol.py` fails the protocol otherwise.

Use `charting_questions` (§ 5) instead for a one-off extraction detail that
isn't a question the review is answering.

## 4. Search settings

| Field | Tier | Question | Default | Validation |
| --- | --- | --- | --- | --- |
| `databases` | [+] | "Which sources to search?" | PubMed, bioRxiv, medRxiv, europe_pmc, openalex, crossref, doaj, semantic_scholar, arxiv, core | subset of the known providers; API-key-gated ones (e.g. core) skipped silently |
| `date_range_start` / `date_range_end` ★ | [+] | "Publication date range?" | empty = unbounded | ISO `YYYY-MM-DD` or year |
| `max_hops` | [+] | "Citation-chasing hops (0–10)?" | 10 | integer 0–10 |
| `rob_tool` ★ | [+] | "Risk-of-bias / appraisal instrument?" | `RoB 2` | one of: RoB 2, Jadad Scale, ROBINS-I, ROBINS-E, Newcastle-Ottawa Scale, QUADAS-2, CASP Qualitative Checklist, JBI Critical Appraisal, Murad Tool, SYRCLE, MINORS, ROBIS |

## 5. Data charting — what is auto-extracted per included article

Every included article is charted into **sections A–G** automatically (no input
needed, but tell the user this is captured so they can add to it):

- **A. Publication** — title, authors, year, journal, DOI, database,
  **`disorder_cohort`**, primary focus.
- **B. Study design** — goal, design, duration, subject model, task type,
  setting, country/region.
- **C. Participants: disordered group** — diagnosis, assessment,
  **n**, age, gender, comorbidities, medications, severity.
- **D. Participants: healthy controls** — included?, n, matching.
- **E. Data collection** — modalities, instruments, features.
- **F. Features & models** — methods, algorithms, validation, metrics, effects.
- **G. Synthesis fields** — findings, certainty, effect estimates.

### Charting features the user CAN configure

| Field | Tier | Question | Example | Validation |
| --- | --- | --- | --- | --- |
| `charting_questions` | [A] | "Any domain-specific questions to answer for every article?" | ["RQ1.1 Which participant groups are compared?", "RQ2.3 What sequencing method was used?"] | list of strings; become `custom_fields` |
| `charting_template` | [A] | "Field-level extraction constraints?" | (advanced object) | optional; pipeline default if omitted |

`charting_questions` is for extraction details that aren't research questions.
**Anything the review is actually answering belongs in `research_questions`
(§ 3b)**, which carries an id, a theme and a title and drives the reporting.
Plain strings here are charted and reported the same way, just without those —
a leading id (`"RQ2.3 What sequencing method was used?"`) is honoured if you
have one.

## 6. Per-group / cohort analysis

The corpus can be **bucketed by a charting attribute** and synthesised +
Q&A'd per bucket (e.g. per disorder cohort, per study design).

| Field | Tier | Question | Example | Validation |
| --- | --- | --- | --- | --- |
| `grouping_dimension` ★ | [A] | "Bucket the corpus by which attribute?" | `disorder_cohort` (default), `study_design`, `country_region`, `primary_focus`, `task_type` | any string-valued rubric attribute |
| `default_group_questions` | [A] | "Questions to answer for every group?" | ["What is the dominant study design?", "Typical sample size?"] | ≤ 10 questions |
| `per_group_questions` | [A] | "Per-group question overrides?" | {"schizophrenia": ["Which biomarker recurs?"]} | ≤ 10 per group; keys matched case-insensitively |

Empty group questions ⇒ synthesis-only (no per-group Q&A).

## 7. Critical appraisal customization

| Field | Tier | Question | Example | Validation |
| --- | --- | --- | --- | --- |
| `appraisal_domains` | [A] | "Custom appraisal domain names?" | ["Participant and Sample Quality", "Data Collection Quality", "Feature and Model Quality", "Bias and Transparency"] | 1–4 names; unspecified positions keep defaults |
| `critical_appraisal_config` | [A] | "Custom appraisal instrument?" | (advanced object) | optional |

## 8. Output formatting

| Field | Tier | Question | Default | Validation |
| --- | --- | --- | --- | --- |
| `target_audience` | [+] | "Target audience?" | "" | academic journal / policymaker / industry / thesis |
| `word_count_target` | [+] | "Target word count?" | 8000 | positive integer |
| `citation_style` | [+] | "Citation style?" | APA 7 | APA 7 / Vancouver / Harvard / IEEE / Chicago |
| `section_output_formats` | [A] | "Per-section output format overrides?" | {} | values: descriptive / yes_no / table / bullet_list / numeric |
| `rubric_section_config` | [A] | "Full per-section title/order/format config?" | [] | optional list |

## 9. Reporting & registration (PRISMA 2020)

| Field | Tier | Question | Notes |
| --- | --- | --- | --- |
| `registration_number` | [+] | "Protocol registration ID (e.g. PROSPERO)?" | recommended for PRISMA compliance |
| `protocol_url` | [+] | "Protocol URL?" | |
| `funding_sources` | [+] | "Funding sources?" | required by most journals |
| `competing_interests` | [+] | "Competing interests?" | |
| `amendments` | [A] | "Any protocol amendments?" | |

## 10. Cache & processing (usually leave default)

`pg_dsn`, `force_refresh`, `cache_threshold` (0–1, def 0.95),
`cache_ttl_days` (def 30), `share_to_cache` (def true),
`synthesis_batch_size` (def 20), `max_batch_retries` (def 3),
`article_concurrency` (1–20, def 5), `max_articles` (null = no cap),
`review_id` (minted if empty). Only ask if the user raises caching/scale needs.

## 11. Reading budgets — how much of each paper is read

Evidence extraction chunks every included article's full text and processes
**every** chunk, so results and discussion are read rather than just the opening
pages. That is the right default and also the main cost driver: a 1 M-character
survey is ~85 LLM calls on its own. These are the knobs that bound it.

| Field | Tier | Question | Default | Validation |
| --- | --- | --- | --- | --- |
| `evidence_max_chars` | [A] | "Cap how much of each paper is read for evidence?" | 0 = whole article | integer ≥ 0; a value under ~20 000 truncates most papers mid-results and is warned about |
| `evidence_chunk_chars` | [A] | "Characters per evidence-extraction call?" | 12000 | integer ≥ 1000 — smaller = more calls, finer reading |
| `evidence_chunk_overlap` | [A] | "Overlap between chunks?" | 400 | integer ≥ 0; keeps a finding spanning a cut readable |
| `evidence_spans_per_article` | [A] | "Max evidence spans kept per article?" | 8 | integer ≥ 1 |
| `article_text_budget` | [A] | "Full-text budget for RoB / extraction / charting?" | 16000 | integer ≥ 1000; longer bodies are windowed to head + tail so methods *and* results stay visible |

Default silently. Only raise them when the user asks about cost, or when
`run_local_review.py`'s pre-flight reports a chunk count out of proportion to
the review (it prints the estimate before spending anything). Never cap the
reading to save money without telling the user — a review that read the first
few pages of each paper is a different claim from one that read the papers.

---

## Completeness gate

Before running, confirm all **[R]** fields are non-empty and echo a summary back
to the user for confirmation. Run `scripts/validate_protocol.py protocol.json`
— it exits non-zero if a required field is missing and warns on missing
recommended fields and invalid enum values.

## Provenance of the intake

Each answer should be recorded as a `slr:UserInput` inside a
`slr:PreWorkflowSession` (`session_type` = protocol setup), with
`question_asked`, `input_value`, `options_presented`, and `captured_at_time` —
so the review's provenance shows not just the protocol but *how it was
elicited*. See [data_model.md](data_model.md) for the PreWorkflowSession /
UserInput shape.
