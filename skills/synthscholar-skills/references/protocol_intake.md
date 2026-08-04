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
| `charting_questions` | [A] | "Any domain-specific questions to answer for every article?" | ["What sequencing method was used?", "Which diversity index was reported?"] | list of strings; become `custom_fields` |
| `charting_template` | [A] | "Field-level extraction constraints?" | (advanced object) | optional; pipeline default if omitted |

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
`review_id` (minted if empty). Only ask if the user raises caching/scale needs.

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
